# ux-swarm

<!-- badges: pypi version, npm version, python 3.11+ -->

Point a swarm of synthetic users at any URL or screenshot, give them a task, and see their completion rate and pain points.

## Highlights

- **Instant testing** — simulate 20 users in under a minute; no scheduling, no interviews
- **Repeatable** — run before and after a deploy to track whether completion rate improved
- **Real signal** — not a replacement for user testing, but genuine findings at a fraction of the cost and time
- **Customizable personas** — define your own user types and weight the swarm to match your real audience
- **Accessibility included** — screen reader personas ship out of the box

## Overview

ux-swarm is a CLI tool for synthetic UX testing built for frontend developers who want fast, quantifiable signal during development or after launch. Share a URL or screenshot, give it a task, watch a swarm of distinct user personas try to execute it, and review completion rates, feedback, and ranked pain points.

Two modes: **browser swarm** sends each agent into a live Playwright session to navigate a site step-by-step — good for flows like signup, checkout, or cancellations. **Screenshot swarm** runs faster and cheaper, using LLM vision to evaluate an image or UI — perfect for quick reviews and mockups before you have a live URL.

### Authors

Built by [@masonomara](https://github.com/masonomara) at [O'Mara Technology Design](https://omaratechnology.com).

## Usage

<!-- terminal recording placeholder -->

```bash
# browser swarm — live URL
swarm https://example.com complete the checkout flow

# screenshot swarm — image or mockup
swarm mockup.png find where to contact support

# limit agents or steps
swarm https://example.com create an account --users 10 --max-steps 5
```

## Installation

**npm**

```bash
npm install -g ux-swarm
swarm config
```

**pip**

```bash
pip install ux-swarm
swarm config
```

**uv**

```bash
uv tool install ux-swarm
swarm config
```

Requires Python 3.11+. The npm package installs the Python tool underneath — Python must be present on your system. `swarm config` walks through provider selection, API key, model, and Playwright setup on first run.

## Feedback and Contributing

Found a bug or have a feature request? [Open an issue](https://github.com/masonomara/ux-swarm/issues). For questions or ideas, start a [Discussion](https://github.com/masonomara/ux-swarm/discussions).
