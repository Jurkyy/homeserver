# Smart Home Hardware Buying Guide

> A practical, opinionated guide for automating a jungle-themed flat with Home Assistant, MQTT (Mosquitto), and local-first hardware. Cloud-dependent gear is avoided wherever possible.

---

## 1. The Hub: Zigbee Coordinator

Every Zigbee device needs a coordinator. Plug one into your homeserver and it becomes the brain for all Zigbee devices -- no cloud, everything local.

**Top pick: Sonoff Zigbee 3.0 USB Dongle Plus (CC2652P based) -- ~EUR 25**

- Best Home Assistant support via ZHA or Zigbee2MQTT
- Huge device compatibility list
- Just plug it into the homeserver's USB port and configure in HA

**Alternative: SLZB-06 (PoE, Ethernet) -- ~EUR 35**

- Use this if the homeserver is far from where the Zigbee devices live
- Connects over Ethernet instead of USB, so you can place it centrally
- Also works with ZHA and Zigbee2MQTT

> **Verdict:** Get the Sonoff USB dongle. It covers 99% of use cases and costs less. Only go SLZB-06 if your homeserver is tucked away in a closet with poor Zigbee range.

---

## 2. Smart Lighting

Lighting is the single highest-impact automation. It transforms the feel of the flat and is cheap to start with.

### Smart Bulbs (Zigbee)

| Bulb | Price | Type | Notes |
|------|-------|------|-------|
| **IKEA TRADFRI** | ~EUR 8-12 | E27, GU10, white spectrum | Budget king. Great Zigbee mesh participants. Some RGB models available. |
| **Philips Hue** | ~EUR 15-25 | E27, GU10, full range | Works without Hue bridge when paired directly to your Zigbee coordinator. Premium quality. |
| **Innr Zigbee** | ~EUR 20-25 | E27, GU10, full color RGB | Excellent HA support. Best price-to-quality for full RGB. |

**Recommendation:** Start with IKEA TRADFRI for general room lighting (living room, bedroom, kitchen). Add Innr RGB bulbs for accent lighting -- behind plants, inside shelving, or around the projector area to lean into the jungle vibe.

### LED Strips

| Strip | Price | Type | Notes |
|-------|-------|------|-------|
| **BTF-LIGHTING WS2812B + WLED controller (ESP32)** | ~EUR 25-35 for 5m | Addressable RGB | Flash WLED firmware, integrates natively with HA. Full color, amazing effects. |
| **IKEA TRADFRI LED strip** | ~EUR 25 | Warm/cool white only, Zigbee | Dead simple, no soldering, just plug and pair. |

**Where to put them:**

- Behind the projector wall (bias lighting -- reduces eye strain, looks incredible)
- Under shelving with plants (uplight the leaves for that jungle canopy glow)
- Under kitchen cabinets
- Behind the bed headboard
- Along the ceiling edge for ambient wash

**Recommendation:** Go with WS2812B + WLED for the projector wall and any accent areas. The addressable LEDs let you do chase effects, color gradients, and reactive lighting that a simple white strip cannot. WLED integrates directly with HA over WiFi/MQTT.

### Smart Switches (Keep Your Existing Bulbs)

**Sonoff ZBMINI-L2 (Zigbee, no neutral wire needed) -- ~EUR 12**

- Fits behind your existing wall switch
- Perfect for ceiling lights you do not want to (or cannot) replace with smart bulbs
- No neutral wire required -- critical for older European wiring

Use these for hallway ceiling lights, bathroom lights, or any fixture where replacing the bulb is impractical.

---

## 3. Projector Setup

### Projector Recommendations

| Tier | Model | Price | Key Features |
|------|-------|-------|-------------|
| **Budget** | XGIMI Halo+ or Dangbei Neo | ~EUR 400-600 | Built-in Android TV, auto-focus, auto-keystone |
| **Mid-range** | XGIMI Horizon or Epson EF-12 | ~EUR 600-900 | Better brightness, better color accuracy |

**Must-have features:**
- Auto-focus and auto-keystone (you will thank yourself every time you bump the table)
- 1080p minimum (4K is nice but doubles the price for minimal gain at projector distances)
- Short throw if space is limited
- Built-in Android TV saves you an external streaming device

### Controlling the Projector from HA

There are three paths depending on what your projector supports:

1. **HDMI-CEC** (best): If your projector supports CEC, HA can send power on/off and input switching commands through the HDMI chain. Free, no extra hardware.
2. **Network control**: Many projectors expose a LAN/WiFi API. Check Home Assistant integrations for your model. XGIMI and Epson both have community integrations.
3. **IR blaster** (fallback): **Broadlink RM4 Mini -- ~EUR 25**. Learns your projector's IR remote commands and replays them on command from HA. Works with any projector that has an IR remote.

### Screen

A plain white wall works fine for casual use. If you want better image quality, a pull-down screen costs EUR 50-100 and makes a noticeable difference in contrast.

### Ambient Lighting

Put a WS2812B LED strip (running WLED) behind the projector or screen. Set it to a dim warm white or sync it to content using HA automations. This is bias lighting -- it reduces eye strain in dark rooms and looks fantastic.

---

## 4. Audio / Music

### Option A: Smart Speakers (Easiest)

| Speaker | Price | Notes |
|---------|-------|-------|
| **IKEA SYMFONISK** (bookshelf or picture frame) | ~EUR 100-130 | Sonos hardware inside at IKEA prices. Excellent HA integration. |
| **Sonos** (One, Era 100) | ~EUR 200+ | Premium multi-room audio. Best-in-class HA integration. |
| **Google Nest** speakers | ~EUR 50-100 | Cast integration with HA. Cloud-dependent (the one exception). |

**Recommendation:** IKEA SYMFONISK is the sweet spot. You get Sonos quality and ecosystem at half the price. Buy two for stereo or put one in each room for multi-room audio.

### Option B: DIY Multi-Room Audio (Most Flexible)

- Raspberry Pi (or spare SBC) + HiFiBerry DAC HAT + good passive/active speakers
- Run **Snapcast** for synchronized multi-room audio
- Feed it from **Navidrome** (self-hosted music library) or Spotify Connect
- Full HA integration via Snapcast and media_player entities

This is the most powerful option if you already have good speakers or want to use proper bookshelf speakers. More effort to set up, but no subscription fees and total control.

### Soundbar for the Projector Area

Connect a soundbar via HDMI ARC or optical to the projector. Most soundbars can also be controlled via HA (CEC, IR through Broadlink, or network). This keeps movie audio separate from your multi-room music system.

---

## 5. Sensors

Sensors are what make your home actually *smart* instead of just *remote-controlled*.

| Sensor | Price | Use Case |
|--------|-------|----------|
| **IKEA TRADFRI motion sensor** | ~EUR 10 | Hallway auto-light, bathroom auto-light, welcome home |
| **Aqara Motion Sensor P1** | ~EUR 15 | More configurable than IKEA, adjustable sensitivity and timeout |
| **Aqara Door/Window Sensor** | ~EUR 10 | Welcome home trigger (front door), away mode detection |
| **Aqara Temperature/Humidity Sensor** | ~EUR 12 | Monitor plant-friendly conditions, bathroom humidity alerts |

**Automation ideas:**

- **Welcome home**: Door sensor triggers -> lights on at appropriate brightness for time of day, music starts playing
- **Hallway motion**: Motion detected -> lights on for 3 minutes, then off
- **Bathroom humidity**: Humidity above threshold -> remind to open window or trigger fan
- **Plant monitoring**: Track temperature and humidity near your plants, alert if conditions drop out of the ideal range (your jungle will thank you)
- **Circadian lighting**: Light level drops outside -> gradually warm and dim indoor lights

Most motion sensors include a built-in light level sensor, so you do not need a separate one.

---

## 6. Smart Plugs

**IKEA TRADFRI smart plug -- ~EUR 10** or **Sonoff S26R2 Zigbee -- ~EUR 12**

Smart plugs are the cheapest way to automate dumb devices. Use them for:

- String lights and fairy lights (schedule on at sunset, off at midnight)
- Dumb accent lamps
- Humidifier (automate based on humidity sensor readings)
- Fan (automate based on temperature)
- Christmas/decorative lights

Some models (like the Sonoff) include power monitoring, which lets you track energy usage per device in HA.

---

## 7. Optional Cool Additions

These are not essentials, but each one adds a noticeable quality-of-life improvement.

### Smart Curtains/Blinds
- **Aqara Curtain Controller** -- ~EUR 50 (retrofits existing curtain rail)
- **IKEA FYRTUR blinds** -- ~EUR 130 (complete Zigbee blind)
- Auto-open in the morning as a gentle wake-up, auto-close at sunset for privacy

### Automated Plant Watering (On-Theme)
- **DIY**: ESP32 + capacitive moisture sensor + small 5V pump -- ~EUR 15 total
- Flash ESPHome firmware, integrate with HA
- Water your jungle automatically based on soil moisture readings
- **Commercial alternative**: Gardena Smart Water Control for outdoor/balcony plants

### Air Quality Monitoring
- **IKEA Vindstyrka** -- ~EUR 30 (PM2.5, TVOC, temp, humidity -- Zigbee)
- **Aqara TVOC Air Quality Monitor** -- ~EUR 25
- Useful for knowing when to ventilate, especially with lots of plants

### Smart Lock
- **Nuki Smart Lock 3.0 Pro** -- ~EUR 150-200
- Excellent Home Assistant integration (Bluetooth + WiFi bridge)
- Keyless entry, auto-unlock when you arrive home, guest access codes
- Does not replace your existing lock cylinder -- mounts on the inside

### E-Ink Dashboard
- A small e-ink display by the front door showing weather, calendar, and device status
- Build one with an ESP32 + Waveshare e-ink panel, or repurpose an old tablet
- ~EUR 80 for parts

---

## 8. Recommended Starter Kit (Shopping List)

### Phase 1 -- Essentials (~EUR 150-200)

Get the foundation in place. This alone will make the flat feel dramatically different.

| Item | Price | Purpose |
|------|-------|---------|
| Sonoff Zigbee 3.0 USB Dongle Plus | ~EUR 25 | Zigbee coordinator |
| 5x IKEA TRADFRI E27 bulbs | ~EUR 50 | Main room lighting |
| 2x IKEA TRADFRI smart plugs | ~EUR 20 | String lights + accent lamp |
| 1x Broadlink RM4 Mini | ~EUR 25 | Projector IR control |
| 1x WS2812B 5m strip + WLED ESP32 | ~EUR 35 | Accent LED strip (projector wall) |
| 1x IKEA TRADFRI motion sensor | ~EUR 10 | Hallway auto-light |
| **Total** | **~EUR 165** | |

### Phase 2 -- Comfort (~EUR 150-250)

Expand coverage and add intelligence.

| Item | Price | Purpose |
|------|-------|---------|
| 3x more TRADFRI bulbs | ~EUR 30 | Bedroom, kitchen, bathroom |
| 2x Aqara door/window sensors | ~EUR 20 | Welcome home automation |
| 1x Aqara temp/humidity sensor | ~EUR 12 | Plant environment monitoring |
| 1x IKEA SYMFONISK speaker | ~EUR 100 | Multi-room music |
| 1x more WS2812B strip + WLED | ~EUR 30 | Behind bed or under shelving |
| **Total** | **~EUR 192** | |

### Phase 3 -- Premium (~EUR 200+)

Quality-of-life upgrades once the basics are solid.

| Item | Price | Purpose |
|------|-------|---------|
| Smart curtains/blinds | ~EUR 130 | Auto light management, gentle wake-up |
| Smart lock (Nuki) | ~EUR 150 | Keyless entry |
| More speakers | ~EUR 100+ | Full multi-room audio |
| E-ink dashboard | ~EUR 80 | Wall-mounted status display |
| **Total** | **~EUR 460+** | |

---

## 9. Network Considerations

- **Zigbee mesh**: Every mains-powered Zigbee device (bulbs, plugs, switches) acts as a router. More devices = better mesh coverage. Battery devices (sensors) are end-nodes only.
- **WiFi/MQTT devices**: WLED controllers and ESP32 DIY devices connect over WiFi. Keep them on a **separate VLAN** if your router supports it (most UniFi, MikroTik, and OpenWrt routers do).
- **Remote access**: Use **Tailscale** for secure remote access to HA without port forwarding. It is free for personal use and takes 5 minutes to set up.
- **Avoid cloud bridges**: Every cloud dependency is a single point of failure and a privacy leak. The entire point of this setup is that everything works when the internet is down.

---

## 10. Where to Buy (Europe)

| Store | Best For |
|-------|----------|
| **IKEA** (in-store or online) | TRADFRI bulbs, plugs, sensors, SYMFONISK speakers -- best budget smart home gear |
| **Amazon.de / Amazon.nl** | Aqara sensors, Sonoff dongles, Broadlink, Innr bulbs |
| **AliExpress** | ESP32 boards, WS2812B LED strips, cheap Zigbee devices (longer shipping, 2-4 weeks) |
| **Bol.com** | General electronics (Netherlands) |
| **MediaMarkt** | Projectors, speakers, when you want it same-day |
| **Berrybase / BuyDisplay** | Raspberry Pi accessories, e-ink displays, HiFiBerry DACs |

**Before buying anything**, check compatibility at: https://home-assistant.io/integrations/

For Zigbee device compatibility specifically, check: https://zigbee.blakadder.com/

---

## Quick Reference: Software Stack

| Component | Software | Role |
|-----------|----------|------|
| Home Assistant | Core automation platform | Runs on the homeserver |
| Mosquitto | MQTT broker | Already running, handles MQTT devices |
| Zigbee2MQTT (or ZHA) | Zigbee coordinator software | Bridges Zigbee devices to MQTT/HA |
| WLED | LED strip firmware | Runs on ESP32, controls addressable LEDs |
| ESPHome | DIY sensor firmware | For any custom ESP32 projects (plant watering, etc.) |
| Snapcast | Multi-room audio sync | Optional, for DIY speaker setups |
| Navidrome | Self-hosted music server | Optional, for local music library |

---

*Last updated: 2026-03-26*
