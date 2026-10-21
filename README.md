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

## Feature 1: Explore the Orbit

Display satellites and their predicted ground tracks on an interactive **Three.js globe**. Try:

``` JSON
Show the ISS, Tiangong and Hubble
```

The globe displays the satellites with their names and NORAD IDs. Responses describe computed position and altitude; single-satellite requests also provide details such as speed, orbital period and whether the satellite is sunlit.

``` JSON
Where is NORAD 25544 right now?
```

Rotate the globe to explore the geometry, switch between views or select a satellite to request its details.

## Feature 2: Find Your Next Sighting

Set **My location** to a town or city, then ask:

``` JSON
When can I see the ISS?
```

The **My Sky** view shows the predicted pass relative to the observer. The accompanying answer describes the local time, compass direction, peak elevation and pass duration.

> [!NOTE]
> These are geometric visibility estimates — not a guarantee that an object will be visible to the naked eye.

## Feature 3: Identify Objects by NORAD ID

A **NORAD catalogue ID** identifies a catalogued object unambiguously. It is an identifier, not an orbit classification or a guarantee that current orbital elements are available.

Examples:

| NORAD ID | Object                                |
| -------- | ------------------------------------- |
| `25544`  | International Space Station           |
| `48274`  | Tiangong space station core module    |
| `20580`  | Hubble Space Telescope                |
| `33591`  | NOAA 19                               |
| `00005`  | Vanguard 1                            |
| `43205`  | Tesla Roadster / Falcon Heavy payload |

When an ID has no current orbital elements in the application’s catalogue, OrbitSim queries **Space-Track SATCAT metadata** to identify the object. You may try:

``` JSON
Show 20580 and 33591
```

> [!Note]
> Identifying an object and visualising its trajectory are different capabilities. Objects such as Tesla Roadster and James Webb Space Telescope are not suitable for this Earth-orbit viewer, even though they have NORAD catalogue IDs.

## Demo and Resources

- **YouTube Video:** [OrbitSim Demo](https://youtu.be/SlpDfo8wmII)
- **Docker Image:** [GHCR Repository](https://ghcr.io/lazauk/orbitsim-ai:latest)
- **Satellite Catalogue:** [Space-Track.org](https://www.space-track.org/)
