## Click Conventions

We are using the CLick Python library. Click encourages a specific way of designing CLI tools. The conventiosn taht CLick encourages are summarized here:

- Commands have arguments and options. Arguments are positional—they are strings that you pass directly to the command, like data.db in datasette data.db. Arguments can be required or optional, and you can have commands which accept an unlimited number of arguments.
- Options are, usually, optional. They are things like --port 8000. Options can also have a single character shortened version, such as -p 8000.
  - Very occasionally I’ll create an option that is required, usually because a command has so many positional arguments that forcing an option makes its usage easier to read.
- Some options are flags—they don’t take any additional parameters, they just switch something on. shot-scraper --retina is an example of this.
- Flags with single character shortcuts can be easily combined—symbex -in fetch_data is short for symbex --imports --no-file fetch_data [for example](https://github.com/simonw/symbex/blob/1.4/README.md#usage).
- Some options take multiple parameters. `datasette --setting sql_time_limit_ms 10000` is an example, taking both the name of the setting and the value it should be set to.
- Commands can have sub-commands, each with their own family of commands. [llm templates](https://llm.datasette.io/en/stable/templates.html) is an example of this, with `llm templates list` and `llm templates show` and [several more](https://llm.datasette.io/en/stable/help.html#llm-templates-help).
- Every command should have help text—the more detailed the better. This can be viewed by running `llm --help—or` for sub-commands, `llm templates --help`.
