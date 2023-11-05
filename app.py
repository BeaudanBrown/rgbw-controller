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
import random

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

COL_MOD_FADE_TIME = 0.15
COL_MOD_DELAY_TIME = 0.15
FADE_TIME = 0.75
KNOB_TIMEOUT_SECONDS = 10
INTERVALS = 300
R = max((INTERVALS * math.log(2,10)) / math.log(255,10), 0.1)

knobTimeout = datetime.utcnow()
knobState = KnobState.DEFAULT
isHeld = False
singlePress = False

class Colour(BaseModel):
    red: float = 0
    green: float = 0
    blue: float = 0
    white: float = 0

class AuroraSettings(BaseModel):
    storedColour: Colour = Colour()
    minColour: Colour = Colour()
    maxColour: Colour = Colour()
    minColourDist: float = 0.4

class State(BaseModel):
    on: bool = True
    power: int = 100
    presets: list[Colour] = [Colour(white=100)]
    presetIdx: int = 0
    aurora: Optional[AuroraSettings] = None

    def duplicate(self):
        return State(presets=self.presets, presetIdx=self.presetIdx, on=self.on, power=self.power)

class Task(BaseModel):
    fadeTime: float = 0
    postDelay: float = 0
    flash: bool = False

class Power(Task):
    value: int

class Adjustment(Task):
    power: int = 0
    colour: Colour = Colour()

class ChangePreset(Task):
    pass

class Switch(Task):
    pass

class StateChange(State, Task):
    red: Optional[float] = None
    green: Optional[float] = None
    blue: Optional[float] = None
    white: Optional[float] = None
    on: Optional[bool] = None
    power: Optional[int] = None

class Aurora(Task):
    minColour: Colour = Colour()
    maxColour: Colour = Colour()
    minColourDist: float = 0.4

def getEffectivePower(state: State) -> int:
    return state.power if state.on else 0

def getPwmColour(maxColourVal: float, effectivePower: int, colourVal: float) -> int:
    effectiveColour = 0 if maxColourVal == 0 else colourVal / maxColourVal * 100
    return bound(0, 255, (effectiveColour * effectivePower * 255) / 10000)

def normalizeColour(colour: Colour):
    vector = [colour.red, colour.green, colour.blue, colour.white]
    magnitude = math.sqrt(sum([component**2 for component in vector]))
    if magnitude == 0:
        return colour
    normalizedVec = [component/magnitude for component in vector]
    return Colour(red=normalizedVec[0], green=normalizedVec[1], blue=normalizedVec[2], white=normalizedVec[3], )

unitWhite = normalizeColour(Colour(red=1, green=1, blue=1, white=0))

def colourDist(c1: Colour, c2: Colour):
    return math.sqrt((c1.red-c2.red) ** 2 + (c1.green-c2.green) ** 2 + (c1.blue-c2.blue) ** 2 + (c1.white-c2.white) ** 2)

def applyTask(task, currentTarget: State) -> State:
    targetState = currentTarget.duplicate()

    # If we are exiting the Aurora mode then make sure to return the preset to its original value
    if type(task) is not Aurora and targetState.aurora is not None:
        targetState.presets[targetState.presetIdx] = targetState.aurora.storedColour
        targetState.aurora = None

    if type(task) is Adjustment:
        newColour = targetState.presets[targetState.presetIdx]
        if currentTarget.on or (task.colour.red <= 0 and task.colour.green <= 0 and task.colour.blue <= 0 and task.colour.white <= 0):
            newColour.red = bound(0, 100, newColour.red + task.colour.red)
            newColour.green = bound(0, 100, newColour.green + task.colour.green)
            newColour.blue = bound(0, 100, newColour.blue + task.colour.blue)
            newColour.white = bound(0, 100, newColour.white + task.colour.white)
        else:
            # Just set target to the adjustment if we are currently off and have increased a colour
            newColour.red = task.colour.red
            newColour.green = task.colour.green
            newColour.blue = task.colour.blue
            newColour.white = task.colour.white
        targetState.presets[targetState.presetIdx] = newColour
        targetState.on = True
        if currentTarget.on:
            targetState.power = bound(0, 100, currentTarget.power + task.power)
        elif task.power > 0:
            targetState.power = bound(0, 100, task.power)

    elif type(task) is Switch:
        if targetState.power == 0:
            # Default to full power when pressing the switch after dimming to zero
            targetState.power = 100
            targetState.on = True
        else:
            targetState.on = not targetState.on

    elif type(task) is StateChange:
        # StateChange
        newColour = Colour(
            red = currentTarget.presets[currentTarget.presetIdx].red if task.red is None else bound(0, 100, task.red),
            green = currentTarget.presets[currentTarget.presetIdx].green if task.green is None else bound(0, 100, task.green),
            blue = currentTarget.presets[currentTarget.presetIdx].blue if task.blue is None else bound(0, 100, task.blue),
            white = currentTarget.presets[currentTarget.presetIdx].white if task.white is None else bound(0, 100, task.white),
        )
        targetState.presets[targetState.presetIdx] = newColour
        targetState.power = currentTarget.power if task.power is None else bound(0, 100, task.power)
        targetState.on = currentTarget.on if task.on is None else task.on
    elif type(task) is ChangePreset:
        targetState.presetIdx = (targetState.presetIdx + 1) % 3
    elif type(task) is Aurora:
        # If a new aurora is pushed to the queue, make sure to keep the storedColour from the first one
        targetState.aurora = AuroraSettings()
        if currentTarget.aurora is None:
            targetState.aurora.storedColour = currentTarget.presets[currentTarget.presetIdx]
        else:
            targetState.aurora.storedColour = currentTarget.aurora.storedColour
        targetState.aurora.minColour = task.minColour
        targetState.aurora.maxColour = task.maxColour

        validVec: bool = False
        newColour = Colour()

        while not validVec:
            newColour = Colour(
                red = random.randint(int(task.minColour.red), int(task.maxColour.red)),
                green = random.randint(int(task.minColour.green), int(task.maxColour.green)),
                blue = random.randint(int(task.minColour.blue), int(task.maxColour.blue)),
                white = random.randint(int(task.minColour.white), int(task.maxColour.white))
            )

            normalizedCol = normalizeColour(newColour)
            normColMinusWhite = normalizeColour(Colour(red=newColour.red, green=newColour.green, blue=newColour.blue))
            distToWhite = colourDist(normColMinusWhite, unitWhite)
            distToOldTarget = colourDist(normalizedCol, normalizeColour(currentTarget.presets[currentTarget.presetIdx]))
            isOff = newColour.red == 0 and newColour.green == 0 and newColour.blue == 0 and newColour.white == 0

            validVec = distToWhite >= task.minColourDist and distToOldTarget >= task.minColourDist and not isOff

        targetState.presets[targetState.presetIdx] = newColour
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
        presets = list(map(lambda preset : Colour(red=preset["red"], green=preset["green"], blue=preset["blue"], white=preset["white"]), stateJson["presets"]))
        state = State(presets=presets, presetIdx=stateJson["presetIdx"], on=stateJson["on"], power=stateJson["power"])
        f.close()
        return state

def getStateChange(state: State = State(), task: Task = Task()):
    activeColour = state.presets[state.presetIdx]
    return StateChange(red=activeColour.red, green=activeColour.green, blue=activeColour.blue, white=activeColour.white, on=state.on, power=state.power, flash=task.flash, fadeTime=task.fadeTime, postDelay=task.postDelay)

def saveState(state: State):
    with open('./state.json', 'w') as f:
        stateDict = {
            "on": state.on,
            "power": state.power,
            "presets": list(map(lambda colour : { "red": colour.red, "green": colour.green, "blue": colour.blue, "white": colour.white, }, state.presets)),
            "presetIdx": state.presetIdx,
        }
        json.dump(stateDict, f)
        f.close()
        return

@app.post('/change_preset', status_code=200)
async def change_preset():
    global q
    q.put(ChangePreset(fadeTime=0.25))

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
    colour = state.presets[state.presetIdx]
    return "{0} (r:{1}%, g:{2}%, b:{3}%, w:{4}%)".format(powerString, colour.red, colour.green, colour.blue, colour.white)

@app.post('/set_state', status_code=200)
async def set_state(newState: State):
    global q
    state = newState
    q.put(getStateChange(state))
    return newState

@app.post('/aurora', status_code=200)
async def aurora(aurora: Aurora):
    global q
    q.put(aurora)

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
        initialColour = targetState.presets[targetState.presetIdx]
        maxColourVal = max(initialColour.red, max(initialColour.green, max(initialColour.blue, initialColour.white)))
        effectivePower = getEffectivePower(targetState)
        pi.set_PWM_dutycycle(RED_GPIO, getPwmColour(maxColourVal, effectivePower, initialColour.red))
        pi.set_PWM_dutycycle(GREEN_GPIO, getPwmColour(maxColourVal, effectivePower, initialColour.green))
        pi.set_PWM_dutycycle(BLUE_GPIO, getPwmColour(maxColourVal, effectivePower, initialColour.blue))
        pi.set_PWM_dutycycle(WHITE_GPIO, getPwmColour(maxColourVal, effectivePower, initialColour.white))

        while True:
            if(self.stopped()):
                print("Stopping")
                # If we are stopping the loop, make sure to disable the aurora effect properly
                if targetState.aurora is not None:
                    targetState.presets[targetState.presetIdx] = targetState.aurora.storedColour
                    targetState.aurora = None
                return
            try:
                task = q.get_nowait()

                startRed = pi.get_PWM_dutycycle(RED_GPIO)
                startGreen = pi.get_PWM_dutycycle(GREEN_GPIO)
                startBlue = pi.get_PWM_dutycycle(BLUE_GPIO)
                startWhite = pi.get_PWM_dutycycle(WHITE_GPIO)

                initialState = targetState.duplicate()
                targetState = applyTask(task, targetState)
                targetColour = targetState.presets[targetState.presetIdx]
                maxColourVal = max(targetColour.red, max(targetColour.green, max(targetColour.blue, targetColour.white)))
                effectivePower = getEffectivePower(targetState)
                targetRed = getPwmColour(maxColourVal, effectivePower, targetColour.red)
                targetGreen = getPwmColour(maxColourVal, effectivePower, targetColour.green)
                targetBlue = getPwmColour(maxColourVal, effectivePower, targetColour.blue)
                targetWhite = getPwmColour(maxColourVal, effectivePower, targetColour.white)
            except Empty:
                sleep(0.05)
                continue

            startTime = datetime.utcnow()
            dt = datetime.utcnow() - startTime
            brokeEarly = False
            while dt.total_seconds() <= task.fadeTime:
                brokeEarly = self.stopped() or not q.empty()
                if(brokeEarly):
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

            if brokeEarly:
                continue

            # task.fadeTime has elapsed, ensure target is reached
            pi.set_PWM_dutycycle(RED_GPIO, targetRed)
            pi.set_PWM_dutycycle(GREEN_GPIO, targetGreen)
            pi.set_PWM_dutycycle(BLUE_GPIO, targetBlue)
            pi.set_PWM_dutycycle(WHITE_GPIO, targetWhite)
            sleep(task.postDelay)

            if targetState.aurora is not None and q.empty():
                # Aurora mode is enabled, do another aurora cycle
                if not q.empty():
                    print("This is a strange situation")
                q.put(task)
                continue

            if task.flash:
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
            q.put(StateChange(power=10, flash=True, fadeTime=COL_MOD_FADE_TIME))
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
                q.put(StateChange(red=0, green=100, blue=0, white=0, flash=True, postDelay=COL_MOD_DELAY_TIME, fadeTime=COL_MOD_FADE_TIME))
                knobState = KnobState.MOD_GREEN
            elif knobState == KnobState.MOD_GREEN:
                q.put(StateChange(red=0, green=0, blue=100, white=0, flash=True, postDelay=COL_MOD_DELAY_TIME, fadeTime=COL_MOD_FADE_TIME))
                knobState = KnobState.MOD_BLUE
            elif knobState == KnobState.MOD_BLUE:
                q.put(StateChange(red=0, green=0, blue=0, white=100, flash=True, postDelay=COL_MOD_DELAY_TIME, fadeTime=COL_MOD_FADE_TIME))
                knobState = KnobState.MOD_WHITE
            else:
                q.put(StateChange(red=100, green=0, blue=0, white=0, flash=True, postDelay=COL_MOD_DELAY_TIME, fadeTime=COL_MOD_FADE_TIME))
                knobState = KnobState.MOD_RED
            knobTimeout = datetime.utcnow() + timedelta(seconds = KNOB_TIMEOUT_SECONDS)

def button_held():
    global isHeld
    global q
    global knobState
    isHeld = True
    if knobState != KnobState.DEFAULT:
        knobState = KnobState.DEFAULT
        q.put(StateChange(power=10, flash=True, fadeTime=COL_MOD_FADE_TIME))
    else:
        q.put(ChangePreset(fadeTime=0.25))

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
                q.put(StateChange(red=100, green=0, blue=0, white=0, flash=True, postDelay=COL_MOD_DELAY_TIME, fadeTime=COL_MOD_FADE_TIME))
                knobTimeout = datetime.utcnow() + timedelta(seconds = KNOB_TIMEOUT_SECONDS)
                Timer(KNOB_TIMEOUT_SECONDS, check_knob_timeout).start()
            else:
                knobState = KnobState.DEFAULT
                q.put(StateChange(power=10, flash=True, fadeTime=COL_MOD_FADE_TIME))
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
