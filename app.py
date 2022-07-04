from RPi import GPIO
from time import sleep
from queue import Queue, Empty
from typing import Union
from datetime import datetime

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

import pigpio
import math
import json

from threading import Event, Timer, Thread
from gpiozero import RotaryEncoder, Button

app = FastAPI()

pi = pigpio.pi()

HOLD_TIME = 0.5
DOUBLE_CLICK_TIME = 0.2
RED_GPIO = 26
GREEN_GPIO = 19
BLUE_GPIO = 13
WHITE_GPIO = 6

SW_GPIO = 2
DT_GPIO = 3
CLK_GPIO = 4

FADE_TIME_SECONDS = 0.75
FADE_TIME = FADE_TIME_SECONDS * 1000000
INTERVALS = 300
R = max((INTERVALS * math.log(2,10)) / math.log(255,10), 0.1)

isHeld = False
singlePress = False

class Power(BaseModel):
    value: int

class Adjustment(BaseModel):
    power: int = 0
    red: int = 0
    green: int = 0
    blue: int = 0
    white: int = 0
    fade_time: int = 0

class Switch(BaseModel):
    fade_time: int = FADE_TIME

class State(BaseModel):
    red: int = 0
    green: int = 0
    blue: int = 0
    white: int = 100
    on: bool = True
    power: int = 100

    def duplicate(self):
        return State(red=self.red, green=self.green, blue=self.blue, white=self.white, on=self.on, power=self.power)

def getEffectivePower(state: State):
    return state.power if state.on else 0

def getPwmRed(state: State):
    maxColourVal = max(state.red, max(state.green, max(state.blue, state.white)))
    effectiveRed = 0 if maxColourVal == 0 else state.red / maxColourVal * 100
    return bound(0, 255, (effectiveRed * getEffectivePower(state) * 255) / 10000)

def getPwmGreen(state: State):
    maxColourVal = max(state.red, max(state.green, max(state.blue, state.white)))
    effectiveGreen = 0 if maxColourVal == 0 else state.green / maxColourVal * 100
    return bound(0, 255, (effectiveGreen * getEffectivePower(state) * 255) / 10000)

def getPwmBlue(state: State):
    maxColourVal = max(state.red, max(state.green, max(state.blue, state.white)))
    effectiveBlue = 0 if maxColourVal == 0 else state.blue / maxColourVal * 100
    return bound(0, 255, (effectiveBlue * getEffectivePower(state) * 255) / 10000)

def getPwmWhite(state: State):
    maxColourVal = max(state.red, max(state.green, max(state.blue, state.white)))
    effectiveWhite = 0 if maxColourVal == 0 else state.white / maxColourVal * 100
    return bound(0, 255, (effectiveWhite * getEffectivePower(state) * 255) / 10000)

def applyTask(task, currentTarget):
    targetState = currentTarget.duplicate()
    if type(task) is Adjustment:
        if currentTarget.on or (task.red <= 0 and task.green <= 0 and task.blue <= 0 and task.white <= 0):
            targetState.red = bound(0, 100, currentTarget.red + task.red)
            targetState.green = bound(0, 100, currentTarget.green + task.green)
            targetState.blue = bound(0, 100, currentTarget.blue + task.blue)
            targetState.white = bound(0, 100, currentTarget.white + task.white)
        else:
            # Just set target to the adjustment if we are currently off and have increased a colour
            targetState.red = task.red
            targetState.green = task.green
            targetState.blue = task.blue
            targetState.white = task.white
        targetState.on = True
        if currentTarget.on:
            targetState.power = bound(0, 100, currentTarget.power + task.power)
        elif task.power > 0:
            targetState.power = bound(0, 100, task.power)

    elif type(task) is Switch:
        if targetState.power == 0:
            # Turn on at 10 power if power is currently 0 to avoid switch with no effect
            targetState.power = 10
            targetState.on = True
        else:
            targetState.on = not targetState.on

    else:
        # StateChange
        targetState.red = bound(0, 100, task.red)
        targetState.green = bound(0, 100, task.green)
        targetState.blue = bound(0, 100, task.blue)
        targetState.white = bound(0, 100, task.white)
        targetState.power = bound(0, 100, task.power)
        targetState.on = task.on
    return targetState

class StateChange(State):
    fade_time: int = FADE_TIME

def bound(low, high, value):
    return max(low, min(high, value))

def lerp(A, B, C):
    return A + C * (B - A)

def loadState():
    with open('./state.json', 'r') as f:
        stateJson = json.load(f)
        state = State(red=stateJson["red"], green=stateJson["green"], blue=stateJson["blue"], white=stateJson["white"], on=stateJson["on"], power=stateJson["power"])
        f.close()
        return state

def getStateChange(state: State):
    return StateChange(red=state.red, green=state.green, blue=state.blue, white=state.white, on=state.on, power=state.power)

def saveState(state: State):
    with open('./state.json', 'w') as f:
        stateDict = {
            "red": state.red,
            "green": state.green,
            "blue": state.blue,
            "white": state.white,
            "on": state.on,
            "power": state.power,
        }
        json.dump(stateDict, f)
        f.close()
        return

@app.post('/switch', status_code=200)
async def switch():
    global q
    isOn = (pi.get_PWM_dutycycle(RED_GPIO) + pi.get_PWM_dutycycle(GREEN_GPIO) + pi.get_PWM_dutycycle(BLUE_GPIO) + pi.get_PWM_dutycycle(WHITE_GPIO)) > 0
    q.put(Switch())
    # FIXME: This can be incorrect if switched on and off quickly
    return "ON" if not isOn else "OFF"

@app.post('/tweak_state', status_code=200)
async def tweak_state(adjustment: Adjustment):
    global q
    adjustment.red = bound(-100, 100, adjustment.red)
    adjustment.green = bound(-100, 100, adjustment.green)
    adjustment.blue = bound(-100, 100, adjustment.blue)
    adjustment.white = bound(-100, 100, adjustment.white)

    q.put(adjustment)

@app.get('/get_state')
async def get_state():
    state = loadState()
    powerString = "{0}% power".format(state.power) if state.on else "OFF"
    return "{0} (r:{1}%, g:{2}%, b:{3}%, w:{4}%)".format(powerString, state.red, state.green, state.blue, state.white)

@app.post('/set_state', status_code=200)
async def set_state(newState: State):
    global q
    state = newState
    q.put(getStateChange(state))
    return newState

@app.on_event("shutdown")
def shutdown():
    global fadeThread
    fadeThread.stop()
    fadeThread.join()

class Fade(Thread):
    def __init__(self):
        Thread.__init__(self)
        self._stop_event = Event()

    def stop(self):
        print("Received stop")
        self._stop_event.set()

    def stopped(self):
        return self._stop_event.is_set()

    def run(self):
        # Make sure PWM dutycycle is always set at least once
        targetState = loadState()
        pi.set_PWM_dutycycle(RED_GPIO, getPwmRed(targetState))
        pi.set_PWM_dutycycle(GREEN_GPIO, getPwmGreen(targetState))
        pi.set_PWM_dutycycle(BLUE_GPIO, getPwmBlue(targetState))
        pi.set_PWM_dutycycle(WHITE_GPIO, getPwmWhite(targetState))

        while True:
            if(self.stopped()):
                return
            try:
                task = q.get_nowait()

                startRed = pi.get_PWM_dutycycle(RED_GPIO)
                startGreen = pi.get_PWM_dutycycle(GREEN_GPIO)
                startBlue = pi.get_PWM_dutycycle(BLUE_GPIO)
                startWhite = pi.get_PWM_dutycycle(WHITE_GPIO)

                targetState = applyTask(task, targetState)
                targetRed = getPwmRed(targetState)
                targetGreen = getPwmGreen(targetState)
                targetBlue = getPwmBlue(targetState)
                targetWhite = getPwmWhite(targetState)
            except Empty:
                sleep(0.1)
                continue

            startTime = datetime.utcnow()
            dt = datetime.utcnow() - startTime
            while dt.microseconds <= task.fade_time:
                if(self.stopped() or not q.empty()):
                    print("Broke early")
                    break

                dt = datetime.utcnow() - startTime
                currentInterval = INTERVALS if task.fade_time == 0 else math.floor(min(1.0, dt.microseconds / task.fade_time) * INTERVALS)

                increasingC = (2.0 ** (currentInterval / R) - 1) / 255.0
                decreasingC = 1.0 - ((2.0 ** ((INTERVALS - currentInterval) / R) - 1) / 255.0)

                redC = increasingC if targetRed > startRed else decreasingC
                greenC = increasingC if targetGreen > startGreen else decreasingC
                blueC = increasingC if targetBlue > startBlue else decreasingC
                whiteC = increasingC if targetWhite > startWhite else decreasingC

                pi.set_PWM_dutycycle(RED_GPIO, lerp(startRed, targetRed, redC))
                pi.set_PWM_dutycycle(GREEN_GPIO, lerp(startGreen, targetGreen, greenC))
                pi.set_PWM_dutycycle(BLUE_GPIO, lerp(startBlue, targetBlue, blueC))
                pi.set_PWM_dutycycle(WHITE_GPIO, lerp(startWhite, targetWhite, whiteC))

            saveState(targetState)

            if dt.microseconds > task.fade_time:
                # task.fade_time has elapsed, ensure target is reached
                pi.set_PWM_dutycycle(RED_GPIO, targetRed)
                pi.set_PWM_dutycycle(GREEN_GPIO, targetGreen)
                pi.set_PWM_dutycycle(BLUE_GPIO, targetBlue)
                pi.set_PWM_dutycycle(WHITE_GPIO, targetWhite)

def check_double_click():
    global singlePress
    global q
    if singlePress:
        q.put(Switch())
        singlePress = False

def button_held(channel):
    global isHeld
    global q
    isHeld = True
    change = StateChange()
    q.put(change)

def button_released(channel):
    global isHeld
    global singlePress

    if not isHeld:
        if singlePress:
            singlePress = False
        else:
            singlePress = True
            Timer(DOUBLE_CLICK_TIME, check_double_click).start()
    isHeld = False

def clockwise_rotation(channel):
    global q
    q.put(Adjustment(power=10))

def counter_clockwise_rotation(channel):
    global q
    q.put(Adjustment(power=-10))

q = Queue()
fadeThread = Fade()
fadeThread.start()
button = Button(SW_GPIO)
rotor = RotaryEncoder(CLK_GPIO, DT_GPIO)
button.hold_time = HOLD_TIME
button.when_held = button_held
button.when_released = button_released
rotor.when_rotated_clockwise = clockwise_rotation
rotor.when_rotated_counter_clockwise = counter_clockwise_rotation
