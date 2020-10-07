from led import Led
import time

BLACK = (0,0,0)
RED = (255,0,0)
GREEN = (0,255,0)
BLUE = (0,0,255)
WHITE = (255,255,255)

l = Led()

for x in range(0,12):
    l.set_led(x, GREEN)
    time.sleep(0.1)

for x in range(0,12):
    l.set_led(x, RED)
    time.sleep(0.1)

for x in range(0,12):
    l.set_led(x, BLUE)
    time.sleep(0.1)

time.sleep(1)
l.fill_leds(BLACK)
time.sleep(1)

l.set_leds([
  RED,
  GREEN,
  BLUE,
  RED,
  GREEN,
  BLUE,
  RED,
  GREEN,
  BLUE,
  RED,
  GREEN,
  BLUE
])

time.sleep(2)

l.set_leds([
  RED,
  RED,
  RED,
  GREEN,
  GREEN,
  GREEN,
  BLUE,
  BLUE,
  BLUE,
  WHITE,
  WHITE,
  WHITE
])

time.sleep(3)

l.fill_leds(BLACK)


