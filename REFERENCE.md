# Reference reels

The `reference/` folder holds finished example reels used as style targets for
the **Style-from-reference** feature (`--style-ref`, or the "Style reference"
box in the web UI). The engine analyzes these clips (cut rate vs. tempo,
flash/whip/black-dip vocabulary, grade statistics) and tunes the output to
match.

## Why they're not in git

These are large video binaries (~238 MB total), so the `.mp4` files are
gitignored and only the integrity manifest is version-controlled:

```
reference/MANIFEST.sha256   # SHA-256 of every reference reel (tracked)
reference/*.mp4             # ignored
```

The files stay on disk locally so the style-reference feature keeps working.

## Verifying the set

```bash
cd reference && shasum -a 256 -c MANIFEST.sha256
```

A reel is intact iff its line prints `OK`.

## Rebuilding the manifest

```bash
cd reference && shasum -a 256 *.mp4 > MANIFEST.sha256
```

## Provenance

The reels were downloaded from **SnapReels.Net** (hence the
`SnapReels.Net_<id>.mp4` filenames); `reel_ed3f8e9f827c.mp4` is a locally
produced example output. They are used only as local style references and are
not redistributed.
