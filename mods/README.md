# Drop your mods here

Every `.jar` in this folder is installed into the game.

* **Fabric mods only** — the session runs Fabric, and Fabric API is added
  automatically when one of your mods needs it.
* The Minecraft version, loader and Java version are worked out from the mods
  themselves, so there is nothing to configure.
* A mod built for another Minecraft version is skipped (and listed as
  "skipped" on the site) — the rest still runs.
* Pack files (`.zip`, `.mrpack`) are not applied here: unzip them and drop the
  mod jars in.
* Deleting a jar removes that mod.

Adding, replacing or deleting any file in this folder stops the running session
and starts a new one with the new mod set. The site picks it up on its own.
