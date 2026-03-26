# Tools Reference

## Home Assistant Integration

You have access to Home Assistant via MCP tools. Use these tools to control the smart home.

### Available MCP Tools

- **ha_control_lights** — Turn on/off lights, set brightness (0-255) and color temperature (2000-6500 kelvin)
- **ha_activate_scene** — Activate a named scene
- **ha_set_house_mood** — Set the house mood via input_select (options: Energize, Focus, Relax, Movie, Party, Sleep)
- **ha_toggle_mode** — Toggle boolean modes: movie_mode, focus_mode, jungle_ambience, away_mode, do_not_disturb
- **ha_control_media** — Play/pause/skip/volume on media players
- **ha_control_projector** — Turn on/off projector, switch inputs
- **ha_get_home_state** — Get current state of entities (lights, sensors, modes, media)
- **ha_send_notification** — Send a notification to HA mobile app

### Entity Naming Convention
- Lights: `light.living_room`, `light.bedroom`, `light.kitchen`, `light.bathroom`, `light.office`, `light.accents`
- Scenes: `scene.jungle_day`, `scene.jungle_evening`, `scene.movie_night`, `scene.cozy_reading`, `scene.party_mode`, `scene.deep_focus`, `scene.romantic`, `scene.morning_energize`, `scene.meditation`
- Input selects: `input_select.house_mood`
- Input booleans: `input_boolean.movie_mode`, `input_boolean.focus_mode`, `input_boolean.jungle_ambience`, `input_boolean.away_mode`, `input_boolean.do_not_disturb`
- Media players: `media_player.navidrome`, `media_player.jellyfin`
- Switches: `switch.projector`
