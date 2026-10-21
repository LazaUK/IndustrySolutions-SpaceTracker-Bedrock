# Voice-enabled Tracker of NORAD-classified space objects around Earth

**OrbitSim AI** is a containerised satellite exploration demo, that presents orbital data of human-made space objects around Earth as an interactive **3D globe** or a **local sky view**, and answers your questions on when to look up.

You can track satellites by name or **NORAD catalogue ID**, learn about upcoming passes over your location and inspect the MCP tool calls behind each interaction.

## 📑 Table of Contents
- [The Satellite Exploration Challenge](#the-satellite-exploration-challenge)
- [Run the Pre-Built Demo](#run-the-pre-built-demo)
- [Feature 1: Explore the Orbit](#feature-1-explore-the-orbit)
- [Feature 2: Find Your Next Sighting](#feature-2-find-your-next-sighting)
- [Feature 3: Identify Objects by NORAD ID](#feature-3-identify-objects-by-norad-id)
- [Demo and Resources](#demo-and-resources)

## The Satellite Exploration Challenge

Satellite catalogues and orbital predictions contain valuable information, but interpreting them often requires specialist terminology, numerical tables and several separate tools.

For someone asking **“Where is the ISS?”** or **“When can I see it from London?”**, the useful answer is visual and personal: show the orbit, explain when to look and indicate where the satellite will cross the sky. **OrbitSim AI** brings those steps together in a compact 3D interface designed around conversational requests and interactive exploration.

## Run the Pre-Built Demo

The pre-built Docker image includes the Python server and browser interface. **Docker is the only local runtime dependency** — there is no need to install Python, Node.js or packages on your machine.

1. Create a free account at [Space-Track.org](https://www.space-track.org/).
2. Set `SPACETRACK_USER` and `SPACETRACK_PASS` in your current OS environment.
3. Start the application with your credentials passed **at runtime**:

``` PowerShell
docker run -d --name orbitsim `
  -p 127.0.0.1:8000:8000 `
  -e SPACETRACK_USER `
  -e SPACETRACK_PASS `
  -v orbitsim-data:/data `
  ghcr.io/lazauk/orbitsim-ai:latest
```

The first catalogue download may take a little time after a satellite request.

> [!NOTE]
> Passing `-e SPACETRACK_USER` and `-e SPACETRACK_PASS` copies their values from the environment of the terminal running Docker.
