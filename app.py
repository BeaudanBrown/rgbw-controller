from RPi import GPIO
from time import sleep
from queue import Queue, Empty

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

isHeld = False
singlePress = False

class Power(BaseModel):
    value: int

class Values(BaseModel):
    red: int
    green: int
    blue: int
    white: int
    power: int

class State(BaseModel):
    red: int
    green: int
    blue: int
    white: int
    on: bool
    power: int

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
    state = loadState()
    state.on = not state.on
    q.put(state)
    return "On" if state.on else "Off"

@app.post('/tweak_power', status_code=200)
async def tweak_power(adjustment: Power):
    global q
    state = loadState()
    if state.on:
        state.power = bound(0, 1.0, state.power + adjustment.value)
    else:
        state.on = True
        state.power = bound(0, 1.0, adjustment.value)
    q.put(state)
    return "{0}, {5}% power: (r:{1}, g:{2}, b:{3}, w:{4})".format("ON" if state.on else "OFF", state.red, state.green, state.blue, state.white, state.power * 100)

@app.post('/tweak_values', status_code=200)
async def tweak_values(values: Values):
    global q
    state = loadState()
    state.red = bound(0, 100, state.red + values.red)
    state.green = bound(0, 100, state.green + values.green)
    state.blue = bound(0, 100, state.blue + values.blue)
    state.white = bound(0, 100, state.white + values.white)

    if state.on:
        state.power = bound(0, 100, state.power + adjustment.value)
    else:
        state.power = bound(0, 100, adjustment.value)

    q.put(state)
    powerString = "{0}% power".format(state.power) if state.on else "0"
    return "{0}: (r:{1}%, g:{2}%, b:{3}%, w:{4}%)".format(powerString, state.red, state.green, state.blue, state.white)

# @app.post('/tweak_colours', status_code=200)
# async def tweak_colours(colours: Colours):
#     global q
#     state = loadState()
#     if state.on:
#         state.red = bound(0, 1.0, state.red + colours.red)
#         state.green = bound(0, 1.0, state.green + colours.green)
#         state.blue = bound(0, 1.0, state.blue + colours.blue)
#         state.white = bound(0, 1.0, state.white + colours.white)
#     else:
#         state.on = True
#         state.red = bound(0, 1.0, colours.red)
#         state.green = bound(0, 1.0, colours.green)
#         state.blue = bound(0, 1.0, colours.blue)
#         state.white = bound(0, 1.0, colours.white)
#     q.put(state)
#     return "{0}, {5}% power: (r:{1}, g:{2}, b:{3}, w:{4})".format("ON" if state.on else "OFF", state.red, state.green, state.blue, state.white, state.power * 100)

@app.get('/get_state')
async def get_state():
    state = loadState()
    return "{0}, {5}% power: (r:{1}, g:{2}, b:{3}, w:{4})".format("ON" if state.on else "OFF", state.red, state.green, state.blue, state.white, state.power * 100)

@app.post('/set_state', status_code=200)
async def set_state(newState: State):
    global q
    state = loadState()
    state = newState
    q.put(state)
    return newState

@app.on_event("shutdown")
def shutdown():
    global fadeThread
    fadeThread.stop()
    fadeThread.join()

class Fade(Thread):
    def __init__(self):
        Thread.__init__(self)
        self.targetState = loadState()
        self._stop_event = Event()

    def stop(self):
        print("Received stop")
        self._stop_event.set()

    def setTarget(self, newTargetState):
        # FIXME: Make this pure
        self.targetState = newTargetState

        if self.targetState.on:
            self.targetRed = self.targetState.red
            self.targetGreen = self.targetState.green
            self.targetBlue = self.targetState.blue
            self.targetWhite = self.targetState.white
            self.targetPower = self.targetState.power
        else:
            self.targetRed = 0
            self.targetGreen = 0
            self.targetBlue = 0
            self.targetWhite = 0
            self.targetPower = 0

        self.currentState = loadState()

        self.startRed = self.currentState.red if self.currentState.on else 0
        self.startGreen = self.currentState.green if self.currentState.on else 0
        self.startBlue = self.currentState.blue if self.currentState.on else 0
        self.startWhite = self.currentState.white if self.currentState.on else 0
        self.startPower = self.currentState.power if self.currentState.on else 0

        redDiff = abs(self.targetRed * self.targetState.power - self.startRed * self.startPower)
        greenDiff = abs(self.targetGreen * self.targetState.power - self.startGreen * self.startPower)
        blueDiff = abs(self.targetBlue * self.targetState.power - self.startBlue * self.startPower)
        whiteDiff = abs(self.targetWhite * self.targetState.power - self.startWhite * self.startPower)
        maxDiff = max(redDiff, max(greenDiff, max(blueDiff, whiteDiff)))

        self.intervals = round(maxDiff * 255)
        self.R = max((self.intervals * math.log(2,10)) / math.log(255,10), 0.1)

    def stopped(self):
        return self._stop_event.is_set()

    def run(self):
        while True:
            if(self.stopped()):
                return
            try:
                newTargetState = q.get_nowait()
            except Empty:
                sleep(0.1)
                continue
            self.setTarget(newTargetState)
            self.currentState.on = self.targetState.on
            for x in range(self.intervals + 1):
                if(self.stopped() or not q.empty()):
                    saveState(self.currentState)
                    break
                increasingC = (2.0 ** (x / self.R) - 1) / 255.0
                decreasingC = 1.0 - ((2.0 ** ((self.intervals - x) / self.R) - 1) / 255.0)

                # redC = increasingC if self.targetRed > self.startRed else decreasingC
                # greenC = increasingC if self.targetGreen > self.startGreen else decreasingC
                # blueC = increasingC if self.targetBlue > self.startBlue else decreasingC
                # whiteC = increasingC if self.targetWhite > self.startWhite else decreasingC
                # powerC = increasingC if self.targetPower > self.startPower else decreasingC

                redC = decreasingC
                greenC = decreasingC
                blueC = decreasingC
                whiteC = decreasingC
                powerC = decreasingC

                self.currentState.red = bound(0, 1.0, lerp(self.startRed, self.targetRed, redC))
                self.currentState.green = bound(0, 1.0, lerp(self.startGreen, self.targetGreen, greenC))
                self.currentState.blue = bound(0, 1.0, lerp(self.startBlue, self.targetBlue, blueC))
                self.currentState.white = bound(0, 1.0, lerp(self.startWhite, self.targetWhite, whiteC))
                self.currentState.power = bound(0, 1.0, lerp(self.startPower, self.targetPower, powerC))

                if x == self.intervals:
                    pi.set_PWM_dutycycle(RED_GPIO, self.targetRed * self.targetState.power * 255)
                    pi.set_PWM_dutycycle(GREEN_GPIO, self.targetGreen * self.targetState.power * 255)
                    pi.set_PWM_dutycycle(BLUE_GPIO, self.targetBlue * self.targetState.power * 255)
                    pi.set_PWM_dutycycle(WHITE_GPIO, self.targetWhite * self.targetState.power * 255)
                    saveState(self.targetState)
                else:
                    pi.set_PWM_dutycycle(RED_GPIO, self.currentState.red * self.currentState.power * 255)
                    pi.set_PWM_dutycycle(GREEN_GPIO, self.currentState.green * self.currentState.power * 255)
                    pi.set_PWM_dutycycle(BLUE_GPIO, self.currentState.blue * self.currentState.power * 255)
                    pi.set_PWM_dutycycle(WHITE_GPIO, self.currentState.white * self.currentState.power * 255)

def check_double_click():
    global singlePress
    global q
    state = loadState()
    if singlePress:
        state.on = not state.on
        q.put(state)
        singlePress = False

def button_held(channel):
    global isHeld
    global q
    state = loadState()
    isHeld = True
    state.red = 0.0
    state.green = 0.0
    state.blue = 0.0
    state.white = 1.0
    state.on = True
    state.power = 1.0
    q.put(state)

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
    state = loadState()
    state.power = 0.1 if not state.on else min(state.power + 0.1, 1)
    state.on = True
    q.put(state)

def counter_clockwise_rotation(channel):
    global q
    state = loadState()
    state.on = True
    state.power = max(state.power - 0.1, 0)
    q.put(state)

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
