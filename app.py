from time import sleep
from queue import Queue, Empty
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

import pigpio
import math
import json

from threading import Event, Timer, Thread
from gpiozero import RotaryEncoder, Button

app = FastAPI()

pi = pigpio.pi()

HOLD_TIME = 0.5
DOUBLE_CLICK_TIME = 0.4
RED_GPIO = 26
GREEN_GPIO = 19
BLUE_GPIO = 13
WHITE_GPIO = 6

SW_GPIO = 2
DT_GPIO = 3
CLK_GPIO = 4

class KnobState(Enum):
    DEFAULT = 1
    MOD_RED = 2
    MOD_GREEN = 3
    MOD_BLUE = 4
    MOD_WHITE = 5

FADE_TIME = 0.75
KNOB_TIMEOUT_SECONDS = 10
INTERVALS = 300
R = max((INTERVALS * math.log(2,10)) / math.log(255,10), 0.1)

knobTimeout = datetime.utcnow()
knobState = KnobState.DEFAULT
isHeld = False
singlePress = False

class Task(BaseModel):
    fadeTime: float = 0
    postDelay: float = 0
    flash: bool = False

class Power(Task):
    value: int

class Colour(BaseModel):
    red: int = 0
    green: int = 0
    blue: int = 0
    white: int = 0

class Adjustment(Task):
    power: int = 0
    colour: Colour = Colour()

class Switch(Task):
    pass

class State(BaseModel):
    colour: Colour = Colour()
    on: bool = True
    power: int = 100

    def duplicate(self):
        return State(colour=self.colour, on=self.on, power=self.power)

class StateChange(State, Task):
    red: Optional[int] = None
    green: Optional[int] = None
    blue: Optional[int] = None
    white: Optional[int] = None
    on: Optional[bool] = None
    power: Optional[int] = None

def getEffectivePower(state: State) -> int:
    return state.power if state.on else 0

def getPwmColour(maxColourVal: int, effectivePower: int, colourVal: int) -> int:
    effectiveColour = 0 if maxColourVal == 0 else colourVal / maxColourVal * 100
    return bound(0, 255, (effectiveColour * effectivePower * 255) / 10000)

def applyTask(task, currentTarget: State) -> State:
    targetState = currentTarget.duplicate()
    if type(task) is Adjustment:
        if currentTarget.on or (task.colour.red <= 0 and task.colour.green <= 0 and task.colour.blue <= 0 and task.colour.white <= 0):
            targetState.colour.red = bound(0, 100, currentTarget.colour.red + task.colour.red)
            targetState.colour.green = bound(0, 100, currentTarget.colour.green + task.colour.green)
            targetState.colour.blue = bound(0, 100, currentTarget.colour.blue + task.colour.blue)
            targetState.colour.white = bound(0, 100, currentTarget.colour.white + task.colour.white)
        else:
            # Just set target to the adjustment if we are currently off and have increased a colour
            targetState.colour.red = task.colour.red
            targetState.colour.green = task.colour.green
            targetState.colour.blue = task.colour.blue
            targetState.colour.white = task.colour.white
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

    elif type(task) is StateChange:
        # StateChange
        targetState.colour.red = currentTarget.red if task.red is None else bound(0, 100, task.red)
        targetState.colour.green = currentTarget.green if task.green is None else bound(0, 100, task.green)
        targetState.colour.blue = currentTarget.blue if task.blue is None else bound(0, 100, task.blue)
        targetState.colour.white = currentTarget.white if task.white is None else bound(0, 100, task.white)
        targetState.power = currentTarget.power if task.power is None else bound(0, 100, task.power)
        targetState.on = currentTarget.on if task.on is None else task.on
    else:
        print("Unknown task type")
    return targetState

def bound(low, high, value):
    return int(max(low, min(high, value)))

def lerp(A, B, C):
    return A + C * (B - A)

def loadState():
    with open('./state.json', 'r') as f:
        stateJson = json.load(f)
        state = State(colour=Colour(red=stateJson["colour"]["red"], green=stateJson["colour"]["green"], blue=stateJson["colour"]["blue"], white=stateJson["colour"]["white"]), on=stateJson["on"], power=stateJson["power"])
        f.close()
        return state

def getStateChange(state: State = State(), task: Task = Task()):
    return StateChange(red=state.colour.red, green=state.colour.green, blue=state.colour.blue, white=state.colour.white, on=state.on, power=state.power, flash=task.flash, fadeTime=task.fadeTime, postDelay=task.postDelay)

def saveState(state: State):
    with open('./state.json', 'w') as f:
        stateDict = {
            "colour": {
                "red": state.colour.red,
                "green": state.colour.green,
                "blue": state.colour.blue,
                "white": state.colour.white,
            },
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
    q.put(Switch(fadeTime=FADE_TIME))
    # FIXME: This can be incorrect if switched on and off quickly
    return "ON" if not isOn else "OFF"

@app.post('/tweak_state', status_code=200)
async def tweak_state(adjustment: Adjustment):
    global q
    adjustment.colour.red = bound(-100, 100, adjustment.colour.red)
    adjustment.colour.green = bound(-100, 100, adjustment.colour.green)
    adjustment.colour.blue = bound(-100, 100, adjustment.colour.blue)
    adjustment.colour.white = bound(-100, 100, adjustment.colour.white)

    q.put(adjustment)

@app.get('/get_state')
async def get_state():
    state = loadState()
    powerString = "{0}% power".format(state.power) if state.on else "OFF"
    return "{0} (r:{1}%, g:{2}%, b:{3}%, w:{4}%)".format(powerString, state.colour.red, state.colour.green, state.colour.blue, state.colour.white)

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
        initialColour = targetState.colour
        maxColourVal = max(initialColour.red, max(initialColour.green, max(initialColour.blue, initialColour.white)))
        effectivePower = getEffectivePower(targetState)
        pi.set_PWM_dutycycle(RED_GPIO, getPwmColour(maxColourVal, effectivePower, initialColour.red))
        pi.set_PWM_dutycycle(GREEN_GPIO, getPwmColour(maxColourVal, effectivePower, initialColour.green))
        pi.set_PWM_dutycycle(BLUE_GPIO, getPwmColour(maxColourVal, effectivePower, initialColour.blue))
        pi.set_PWM_dutycycle(WHITE_GPIO, getPwmColour(maxColourVal, effectivePower, initialColour.white))

        while True:
            if(self.stopped()):
                print("Stopping")
                return
            try:
                task = q.get_nowait()

                startRed = pi.get_PWM_dutycycle(RED_GPIO)
                startGreen = pi.get_PWM_dutycycle(GREEN_GPIO)
                startBlue = pi.get_PWM_dutycycle(BLUE_GPIO)
                startWhite = pi.get_PWM_dutycycle(WHITE_GPIO)

                initialState = targetState.duplicate()
                targetState = applyTask(task, targetState)
                maxColourVal = max(targetState.colour.red, max(targetState.colour.green, max(targetState.colour.blue, targetState.colour.white)))
                effectivePower = getEffectivePower(targetState)
                targetRed = getPwmColour(maxColourVal, effectivePower, targetState.colour.red)
                targetGreen = getPwmColour(maxColourVal, effectivePower, targetState.colour.green)
                targetBlue = getPwmColour(maxColourVal, effectivePower, targetState.colour.blue)
                targetWhite = getPwmColour(maxColourVal, effectivePower, targetState.colour.white)
            except Empty:
                sleep(0.1)
                continue

            startTime = datetime.utcnow()
            dt = datetime.utcnow() - startTime
            while dt.total_seconds() <= task.fadeTime:
                if(self.stopped() or not q.empty()):
                    print("Broke early")
                    break

                dt = datetime.utcnow() - startTime
                currentInterval = INTERVALS if task.fadeTime == 0 else math.floor(min(1.0, dt.total_seconds() / task.fadeTime) * INTERVALS)

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

            if dt.total_seconds() > task.fadeTime:
                # task.fadeTime has elapsed, ensure target is reached
                pi.set_PWM_dutycycle(RED_GPIO, targetRed)
                pi.set_PWM_dutycycle(GREEN_GPIO, targetGreen)
                pi.set_PWM_dutycycle(BLUE_GPIO, targetBlue)
                pi.set_PWM_dutycycle(WHITE_GPIO, targetWhite)

            if task.flash:
                sleep(task.postDelay)
                q.put(getStateChange(initialState, Task(fadeTime = task.fadeTime)))
            else:
                saveState(targetState)

def check_knob_timeout():
    global knobTimeout
    global knobState
    global q
    now = datetime.utcnow()
    if knobTimeout <= now:
        if knobState != KnobState.DEFAULT:
            knobState = KnobState.DEFAULT
            q.put(StateChange(power=10, flash=True, fadeTime=0.15))
    else:
        Timer((knobTimeout - now).total_seconds(), check_knob_timeout).start()

def check_double_click():
    global knobState
    global knobTimeout
    global singlePress
    global q

    if singlePress:
        singlePress = False
        if knobState == KnobState.DEFAULT:
            q.put(Switch(fadeTime=FADE_TIME))
        else:
            if knobState == KnobState.MOD_RED:
                q.put(StateChange(red=0, green=100, blue=0, white=0, flash=True, postDelay=0.5, fadeTime=0.25))
                knobState = KnobState.MOD_GREEN
            elif knobState == KnobState.MOD_GREEN:
                q.put(StateChange(red=0, green=0, blue=100, white=0, flash=True, postDelay=0.5, fadeTime=0.25))
                knobState = KnobState.MOD_BLUE
            elif knobState == KnobState.MOD_BLUE:
                q.put(StateChange(red=0, green=0, blue=0, white=100, flash=True, postDelay=0.5, fadeTime=0.25))
                knobState = KnobState.MOD_WHITE
            else:
                q.put(StateChange(red=100, green=0, blue=0, white=0, flash=True, postDelay=0.5, fadeTime=0.25))
                knobState = KnobState.MOD_RED
            knobTimeout = datetime.utcnow() + timedelta(seconds = KNOB_TIMEOUT_SECONDS)

def button_held():
    global isHeld
    global q
    global knobState
    isHeld = True
    if knobState != KnobState.DEFAULT:
        knobState = KnobState.DEFAULT
        q.put(StateChange(power=10, flash=True, fadeTime=0.15))
    else:
        q.put(getStateChange(task = Task(fadeTime=FADE_TIME)))

def button_released():
    global isHeld
    global singlePress
    global knobState
    global knobTimeout

    if not isHeld:
        if singlePress:
            # Double click has occurred
            singlePress = False
            if knobState == KnobState.DEFAULT:
                knobState = KnobState.MOD_RED
                q.put(StateChange(red=100, green=0, blue=0, white=0, flash=True, postDelay=0.5, fadeTime=0.25))
                knobTimeout = datetime.utcnow() + timedelta(seconds = KNOB_TIMEOUT_SECONDS)
                Timer(KNOB_TIMEOUT_SECONDS, check_knob_timeout).start()
            else:
                knobState = KnobState.DEFAULT
                q.put(StateChange(power=10, flash=True, fadeTime=0.15))
        else:
            # Single click has occurred
            singlePress = True
            Timer(DOUBLE_CLICK_TIME, check_double_click).start()
    isHeld = False

def clockwise_rotation():
    global q
    global knobState
    global knobTimeout

    if knobState == KnobState.DEFAULT:
        q.put(Adjustment(power=10))
    else:
        knobTimeout = datetime.utcnow() + timedelta(seconds = KNOB_TIMEOUT_SECONDS)
        if knobState == KnobState.MOD_RED:
            q.put(Adjustment(colour=Colour(red=5)))
        elif knobState == KnobState.MOD_GREEN:
            q.put(Adjustment(colour=Colour(green=5)))
        elif knobState == KnobState.MOD_BLUE:
            q.put(Adjustment(colour=Colour(blue=5)))
        elif knobState == KnobState.MOD_WHITE:
            q.put(Adjustment(colour=Colour(white=5)))

def counter_clockwise_rotation():
    global q
    global knobState
    global knobTimeout

    if knobState == KnobState.DEFAULT:
        q.put(Adjustment(power=-10))
    else:
        knobTimeout = datetime.utcnow() + timedelta(seconds = KNOB_TIMEOUT_SECONDS)
        if knobState == KnobState.MOD_RED:
            q.put(Adjustment(colour=Colour(red=-5)))
        elif knobState == KnobState.MOD_GREEN:
            q.put(Adjustment(colour=Colour(green=-5)))
        elif knobState == KnobState.MOD_BLUE:
            q.put(Adjustment(colour=Colour(blue=-5)))
        elif knobState == KnobState.MOD_WHITE:
            q.put(Adjustment(colour=Colour(white=-5)))

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
