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
    #FIXME: This can be old state if transition to off/on is still happening
    state = loadState()
    state.on = not state.on
    q.put(getStateChange(state))
    return "On" if state.on else "Off"

@app.post('/tweak_state', status_code=200)
async def tweak_state(adjustment: Adjustment):
    global q
    adjustment.red = bound(-100, 100, adjustment.red)
    adjustment.green = bound(-100, 100, adjustment.green)
    adjustment.blue = bound(-100, 100, adjustment.blue)
    adjustment.white = bound(-100, 100, adjustment.white)
    adjustment.fade_time = 0.1 * 1000000

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
        currentState = loadState()
        targetState = currentState.duplicate()
        while True:
            if(self.stopped()):
                return
            try:
                task = q.get_nowait()
                if type(task) is Adjustment:
                    print("Adjustment")
                    targetState.red = bound(0, 100, targetState.red + task.red) if currentState.on else task.red
                    targetState.green = bound(0, 100, targetState.green + task.green) if currentState.on else task.green
                    targetState.blue = bound(0, 100, targetState.blue + task.blue) if currentState.on else task.blue
                    targetState.white = bound(0, 100, targetState.white + task.white) if currentState.on else task.white
                    targetState.power = bound(0, 100, targetState.power + task.power)
                    targetState.on = True
                    effectiveTargetPower = targetState.power if targetState.on else 0
                else:
                    print("State")
                    targetState.red = bound(0, 100, task.red)
                    targetState.green = bound(0, 100, task.green)
                    targetState.blue = bound(0, 100, task.blue)
                    targetState.white = bound(0, 100, task.white)
                    targetState.power = bound(0, 100, task.power)
                    targetState.on = task.on
                    effectiveTargetPower = targetState.power if task.on else 0
            except Empty:
                sleep(0.1)
                continue

            startTime = datetime.utcnow()
            startState = currentState.duplicate()
            effectiveStartPower = startState.power if startState.on else 0

            redDiff = abs(targetState.red * effectiveTargetPower - startState.red * effectiveStartPower)
            greenDiff = abs(targetState.green * effectiveTargetPower - startState.green * effectiveStartPower)
            blueDiff = abs(targetState.blue * effectiveTargetPower - startState.blue * effectiveStartPower)
            whiteDiff = abs(targetState.white * effectiveTargetPower - startState.white * effectiveStartPower)
            maxDiff = max(redDiff, max(greenDiff, max(blueDiff, whiteDiff)))

            dt = datetime.utcnow() - startTime
            while dt.microseconds <= task.fade_time:
                if(self.stopped() or not q.empty()):
                    # Have to put this in loop to ensure on gets set
                    currentState.on = targetState.on
                    saveState(currentState)
                    print("Broke early")
                    break

                dt = datetime.utcnow() - startTime
                currentInterval = math.floor(min(1.0, dt.microseconds / task.fade_time) * INTERVALS)

                increasingC = (2.0 ** (currentInterval / R) - 1) / 255.0
                decreasingC = 1.0 - ((2.0 ** ((INTERVALS - currentInterval) / R) - 1) / 255.0)

                redC = increasingC if targetState.red > startState.red else decreasingC
                greenC = increasingC if targetState.green > startState.green else decreasingC
                blueC = increasingC if targetState.blue > startState.blue else decreasingC
                whiteC = increasingC if targetState.white > startState.white else decreasingC
                powerC = increasingC if targetState.power > startState.power else decreasingC

                currentState.red = bound(0, 100, lerp(startState.red, targetState.red, redC))
                currentState.green = bound(0, 100, lerp(startState.green, targetState.green, greenC))
                currentState.blue = bound(0, 100, lerp(startState.blue, targetState.blue, blueC))
                currentState.white = bound(0, 100, lerp(startState.white, targetState.white, whiteC))
                currentState.power = bound(0, 100, lerp(startState.power, targetState.power, powerC))
                effectiveCurrentPower = bound(0, 100, lerp(effectiveStartPower, effectiveTargetPower, powerC))

                pi.set_PWM_dutycycle(RED_GPIO, (currentState.red * effectiveCurrentPower * 255)/10000)
                pi.set_PWM_dutycycle(GREEN_GPIO, (currentState.green * effectiveCurrentPower * 255)/10000)
                pi.set_PWM_dutycycle(BLUE_GPIO, (currentState.blue * effectiveCurrentPower * 255)/10000)
                pi.set_PWM_dutycycle(WHITE_GPIO, (currentState.white * effectiveCurrentPower * 255)/10000)

            if q.empty():
                # task.fade_time has elapsed, ensure target is reached
                currentState.red = bound(0, 100, targetState.red)
                currentState.green = bound(0, 100, targetState.green)
                currentState.blue = bound(0, 100, targetState.blue)
                currentState.white = bound(0, 100, targetState.white)
                currentState.power = bound(0, 100, targetState.power)
                effectiveCurrentPower = effectiveTargetPower
                currentState.on = targetState.on
                saveState(targetState)

                pi.set_PWM_dutycycle(RED_GPIO, (currentState.red * effectiveCurrentPower * 255)/10000)
                pi.set_PWM_dutycycle(GREEN_GPIO, (currentState.green * effectiveCurrentPower * 255)/10000)
                pi.set_PWM_dutycycle(BLUE_GPIO, (currentState.blue * effectiveCurrentPower * 255)/10000)
                pi.set_PWM_dutycycle(WHITE_GPIO, (currentState.white * effectiveCurrentPower * 255)/10000)



def check_double_click():
    global singlePress
    global q
    state = loadState()
    if singlePress:
        state.on = not state.on
        q.put(getStateChange(state))
        singlePress = False

def button_held(channel):
    global isHeld
    global q
    state = loadState()
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
    q.put(Adjustment(power=10, fade_time=0.1 * 1000000))

def counter_clockwise_rotation(channel):
    global q
    q.put(Adjustment(power=-10, fade_time=0.1 * 1000000))

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
