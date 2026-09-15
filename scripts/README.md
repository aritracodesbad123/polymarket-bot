# Scripts

- `run_24x7.sh` — runs POLYGROK under `caffeinate` (used by LaunchAgent)
- `install_launch_agent.sh` — install/start macOS LaunchAgent (`com.polygrok.bot`) for 24/7 + auto-restart
- `uninstall_launch_agent.sh` — stop and remove that LaunchAgent

Manual:

```bash
./scripts/install_launch_agent.sh
launchctl print gui/$(id -u)/com.polygrok.bot | head
python -m app.cli status
```
