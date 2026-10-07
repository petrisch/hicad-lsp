# HiCAD Language Server

Attempt to provide real lsp functionality to the HCGS language.

## Hover

- Provide hover functionality for the many `OPTION 1 2` commands.
  Taken from `data/hicad_menus.json`

- Provide hover functionality for the builtin variables.
  Taken from `data/builtin_variables.json`

- Provide a way to document arbitrary variables.

## Configuration

Can be used in any editor that supports the language server protocol.
For example in neovim a minimal config looks like this:

```lua
vim.lsp.config["hicad-lsp"] = {
  cmd = {
    "PATHTO/hicad-lsp/.venv/Scripts/python.exe",
    "PATHTO/hicad-lsp/src/server.py",
  },

  filetypes = { "hicad" },

  root_markers = {
    ".git",
  },
}
```

Given that [hicad-lint](https://github.com/petrisch/hicad-lint) is already installed,
or at least the filetype `hicad` is registered.

## TODO

Make add plugin for neovim, maybe vscode
