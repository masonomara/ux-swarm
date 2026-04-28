here are all my help commands:

```text
(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % swarm help

Usage:
  swarm <target> <task> [options]  shortcut to run swarm immediately
  swarm [options] [command]        properly use all options and commands

  <target>                         url or domain to open in a browser, or a path to a screenshot image
  <task>                           plain-language goal for every agent

Examples:
  swarm example.com find the pricing page
  swarm screenshot.png check out --users 5
  swarm https://app.example.com sign up -u 10 -m 12 -w 1440

Options:
  -b, --browser                    show the browser window during the run
  -h, --help                       display help for command
  -m, --max-steps <integer>        max steps per agent (browser only)  [default: 8]
  -u, --users <integer>            number of simulated users  [default: 20]
  -v, --verbose                    show full tracebacks on error
  -V, --version                    output the version number
  -w, --width <integer>            viewport width in pixels (browser only)  [default: 1280]

Commands:
  config                           configure llm provider, model, and browser
  expand                           view per-agent details from the last run
  help [command]                   display help for command
  results [options]                view saved results
  users [options]                  manage user personas

(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % swarm help config
Usage: swarm config

configure your llm provider, api key, model, and chromium installation.
(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % swarm help expand
Usage: swarm expand

view a full per-agent breakdown of the most recent swarm result.
(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % swarm help help
Usage: swarm help [<command>]

display help for a command.
(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % swarm help results
Usage: swarm results [options]

view saved swarm results from .swarm/results.json.

Options:
  -n <integer>  show only the last n results
(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % swarm help users
Usage: swarm users [options]

list and manage the synthetic user personas agents simulate during a run.

Options:
  --config  write .swarm/users.json to disk for manual editing
(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm %
```

heres what they should look like, note that the spaces between teh descriptions and the commands all match the same space from the main help command


```text
(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % swarm help

Usage:
  swarm <target> <task> [options]  shortcut to run swarm immediately
  swarm [options] [command]        properly use all options and commands

  <target>                         url or domain to open in a browser, or a path to a screenshot image
  <task>                           plain-language goal for every agent

Examples:
  swarm example.com find the pricing page
  swarm screenshot.png check out --users 5
  swarm https://app.example.com sign up -u 10 -m 12 -w 1440

Options:
  -b, --browser                    show the browser window during the run
  -h, --help                       display help for command
  -m, --max-steps <integer>        max steps per agent (browser only) [default: 8]
  -u, --users <integer>            number of simulated users [default: 20]
  -v, --verbose                    show full tracebacks on error
  -V, --version                    output the version number
  -w, --width <integer>            viewport width in pixels (browser only) [default: 1280]

Commands:
  config                           configure llm provider, model, and browser
  expand                           view per-agent details from the last run
  help [command]                   display help for command
  results [options]                view saved results
  users [options]                  manage user personas

(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % swarm help config

Usage:
  swarm config                     configure your llm provider, api key, model, and chromium installation.

(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % swarm help expand

Usage:
  swarm expand                     view a full per-agent breakdown of the most recent swarm result.

(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % swarm help help

Usage:
  swarm help [command]             display help for a command

(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % swarm help results

Usage:
  swarm results [options]          view saved swarm results from .swarm/results.json.

Options:
  -n <integer>                     show only the last n results

(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % swarm help users

Usage:
  swarm users [options]            list and manage the synthetic user personas agents simulate during a run.

Options:
  --config                         write .swarm/users.json to disk for manual editing

(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % 
```
