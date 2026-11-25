# Lumi — AI assistant for hair salons

Voice assistant + business system for a salon: appointments, clients (history, allergies, colour formulas),
staff, services, inventory, cash and reports, automatic alerts, hairstyle design and virtual try-on (coming),
with an animated desktop (Lively Wallpaper) and a one-click installer.

> Keep this repository **private**. Keys and salon data are never stored in the repo.

## Make the installer (on your own Windows PC, free)
1. Code → Download ZIP, extract.
2. Double-click **Crear_instalador.bat** → name of the salon/owner, Gemini key, image key (optional).
3. `dist\Lumi-Setup-<name>.exe` → double-click it on the salon's PC.

Salon data lives in `memory/salon.db` on that PC (daily backups in `memory/backups/`).

## macOS
1. On the Mac: Code → Download ZIP (logged in to GitHub), it unzips by itself in Downloads.
2. Right-click **Instalar en Mac.command** → **Open** → **Open** (only the first time, macOS asks).
3. Answer the small windows (name, language, keys). It installs its own Python + AI components,
   starts at every login in the background (LaunchAgent) with its own animated desktop (mac_wallpaper.py).
   Allow the microphone when macOS asks.
Windows-only extras (global hotkeys, some PC-control tools) are not available on Mac.
