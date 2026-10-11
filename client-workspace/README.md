# BYO client workspace

These files are a source-handoff kit present in a repository clone and the raw
sdist archive. They are deliberately absent from the installed server wheel;
installing either distribution format does not attach a workspace. From the
clone or an extracted sdist, run
`client-workspace/bin/attach-project <project-root>` to copy these versioned
client assets to `<project-root>/.agent-workspace`. Runtime grant files are never
shipped and remain isolated under each attached project's
`.agent-workspace/runtime/` directory. The attached workspace carries its own
`.gitignore`, so Git does not stage anything under that runtime directory.
Refreshing an attachment overlays the versioned BYO paths while preserving
validated unrelated top-level files and nested third-party workspace assets.
For race-safe publication, attachment requires POSIX descriptor-path support
through `/proc/self/fd` or `/dev/fd`; unsupported platforms fail before changing
an existing workspace.

The model-invocable `byo-issue-report` skill classifies conversation-visible
S-4 through S-14 signals and submits the server's exact issue/check-in forms.
The `downgrade` and `mass-erase` skills are human-invocable only. Their helper
delegates to the installed server package's canonical manual-permission protocol
so the client and server cannot silently drift to different schemas.
