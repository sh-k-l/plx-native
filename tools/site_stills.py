#!/usr/bin/env python3
"""The site's and the documentation's simulator-made stills: render them on the pinned Linux stack, and adopt them.

    python3 tools/site_stills.py files [--manual]               # every path the refresh may write / leaves to a Mac
    python3 tools/site_stills.py render --out DIR --bin SIM     # screenshots + variants + glows, then DIR/files + manifest
    python3 tools/site_stills.py compare DIR_A DIR_B            # byte-compare two renders; non-zero if any file differs
    python3 tools/site_stills.py manifest DIR [--runs N]        # DIR/stills.manifest.json (after `compare`)
    python3 tools/site_stills.py adopt DIR [--write] [--expect-rev REV]   # dry run unless --write
    python3 tools/site_stills.py tree-hash [--rev REV]          # the stills' inputs' tree hash
    python3 tools/site_stills.py needs-render [--rev REV] [--force]   # needs_stills=true|false against the committed manifest
    python3 tools/site_stills.py commit-message --tag T --run-url U --source-sha S [--film-manifest M] [--stills-manifest M]

WHICH IMAGES. Exactly the ones a simulator makes, read from the tools that make them and not listed by hand
(`output_files`): every output of every scene in `tests/screenshots/scenes.json` (`tools/screenshots.py`: the
documentation figures under `docs/screenshots/`, the site's close-ups `site/media/closeup-*.jpg` and the link
card `site/media/og-card.jpg`) with the `CREDITS.md` that goes with the figures, the WebP and phone copies
`tools/render-site-variants.py` cuts from the close-ups, and the glows `tools/render-site-glows.py` bakes from
them. Not these: a photograph taken on the television (`docs/screenshots/navblur-transition.jpg`, which no tool
renders), the brand art under `assets/` and `site/icons/`, and the film's own posters and glows
(`tools/site_video.py` owns `site/media/feel-*`).

THE SAME SECURITY MODEL AS THE FILM (docs/agent-reference.md, "How the site's stills update themselves"):
`site-video.yml`'s `stills` job renders its own checkout twice and fails unless the two sets are byte-identical;
its `publish` job runs THIS file from main (never the rendered ref's) on the artifact, which is data. `adopt`
refuses anything that is not `platform: linux-ci`, a file whose sha256 differs from the manifest, a file set that
is not exactly `output_files()`, a run that was not byte-identical, and (`--expect-rev`) an artifact rendered
from other inputs than that revision's. A set byte-identical to the committed one writes nothing, the manifest
included, so the manifest keeps naming the tree the stills were last rendered for.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
SCENES = ROOT / "tests" / "screenshots" / "scenes.json"
MANIFEST_REL = "site/media/stills.manifest.json"
MANIFEST_NAME = "stills.manifest.json"
SCHEMA = 1

# Everything that can change a pixel of a still, as git trees (or blobs) at a revision: every crate, the assets
# and locale strings the app embeds, the C shims, the scene manifest, the demo library and the mock that serves
# it, the card's page, the three fonts the simulator reads out of `pkg/`, and the tools that cut the files.
TREE_PATHS = (
    "rust-modules", "assets", "locales", "src",
    "tests/screenshots", "tests/demo_library", "tests/mock_pms.py", "site/og",
    "pkg/appfont.ttf", "pkg/appfont-bold.ttf", "pkg/appfont-cjk.ttf",
    "tools/screenshots.py", "tools/render-og-card.sh", "tools/render-site-variants.py",
    "tools/render-site-glows.py", "tools/site_stills.py",
)


class Failure(Exception):
    pass


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _site_video():
    return sys.modules.get("site_video") or _load("site_video", "site_video.py")


# Scenes whose stills stay a maintainer's `make screenshots` on a Mac (then `render-site-variants.py` and
# `render-site-glows.py`), each with the reason MEASURED on the runner (run 37845183222, two renders in one job):
#  * the two that PLAY a stream: the host FFmpeg loads and the stream opens, but the engine reports `EOS pushed
#    at true EOF` before the first picture (`player`: no `simvideo: first picture decoded`; `site-up-next`: the
#    playhead never reaches the clock stop), so there is no frame to photograph;
#  * four scenes whose two Linux renders were NOT byte-identical (backdrop blur/glass: a few hundred to a few
#    thousand pixels off by one). The run-twice check is not loosened to take them; `home` takes the link card
#    with it, which is composed around its figure.
# Everything derived from a manual scene's JPEG is manual with it.
MANUAL_SCENES = {
    "player": "playback on the Linux runner ends at EOF before the first decoded picture",
    "site-up-next": "playback on the Linux runner ends at EOF before the playhead reaches the clock stop",
    "home": "two Linux renders differ (81 pixels); the link card is composed around it",
    "account-menu": "two Linux renders differ (1084 pixels)",
    "detail": "two Linux renders differ (452 pixels)",
    "library-grid": "two Linux renders differ (2679 pixels)",
}
DESTS = {"docs": "docs/screenshots", "site": "site/media"}


def scene_table(root=ROOT):
    return json.loads((root / "tests" / "screenshots" / "scenes.json").read_text())["scenes"]


def automated_scenes(root=ROOT):
    return [s["name"] for s in scene_table(root) if s["name"] not in MANUAL_SCENES]


def output_files(root=ROOT, manual=False):
    """Every repo-relative path the refresh may write, sorted, read from the generating tools. `manual=True`
    lists instead the stills a Mac maintainer still makes by hand (`MANUAL_SCENES` and their derived copies)."""
    scenes = scene_table(root)
    gone = {o["file"] for s in scenes if s["name"] in MANUAL_SCENES for o in s["outputs"]}
    source = {"docs/screenshots/CREDITS.md": None}  # path -> the scene output it is cut from
    for scene in scenes:
        for out in scene["outputs"]:
            source[f"{DESTS[out.get('dest', 'docs')]}/{out['file']}"] = out.get("card", out["file"])
    variants = _load("render_site_variants", "render-site-variants.py")
    for name in variants.RENDERED:
        source["site/media/" + name.removesuffix(".jpg") + ".webp"] = name
    for src, stem, _ in variants.NARROW:
        source[f"site/media/{stem}.jpg"] = source[f"site/media/{stem}.webp"] = src
    glows = _load("render_site_glows", "render-site-glows.py")
    for g in glows.GLOWS:
        if not g[0].startswith("feel-"):
            source[f"site/media/{g[0]}"] = g[1]
    return sorted(p for p, src in source.items() if (src in gone) == manual)


def glow_names():
    """The close-up glows (`render-site-glows.py`'s table minus the film's own `feel-*`)."""
    glows = _load("render_site_glows", "render-site-glows.py")
    return [g[0] for g in glows.GLOWS if not g[0].startswith("feel-")]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tree_hashes(run=subprocess.run, root=ROOT, rev="HEAD"):
    out = {}
    for p in TREE_PATHS:
        done = run(["git", "-C", str(root), "rev-parse", f"{rev}:{p}"], capture_output=True, text=True)
        if done.returncode != 0:
            raise Failure(f"cannot read the git object of {p} at {rev}: {done.stderr.strip()}")
        out[p] = done.stdout.strip()
    out["combined"] = hashlib.sha256("".join(out[p] for p in TREE_PATHS).encode()).hexdigest()
    return out


def needs_render(rev="HEAD", force=False, manifest_path=None, run=subprocess.run, root=ROOT):
    """(render?, why): yes when forced, when the committed manifest is unreadable, or when its recorded
    `tree_hash.combined` differs from the one at `rev` (same inputs, same stills: the committed ones stand)."""
    if force:
        return True, "forced"
    path = pathlib.Path(manifest_path) if manifest_path else root / MANIFEST_REL
    try:
        recorded = json.loads(path.read_text())["tree_hash"]["combined"]
    except (OSError, ValueError, KeyError, TypeError):
        return True, f"no readable tree hash in {path.name}"
    here = tree_hashes(run, root, rev)["combined"]
    if here == recorded:
        return False, f"the stills' inputs at {rev} are the committed stills' (tree hash {here[:12]})"
    return True, f"the stills' inputs changed: committed {recorded[:12]}, {rev} {here[:12]}"


def render(out, sim_bin, root=ROOT, run=subprocess.run):
    """Render every still into the checkout (this is a disposable runner's tree), then copy the set to
    `out/files/<repo path>`. The previous outputs are deleted first, so a file a run failed to write can never
    be last run's. Returns the list of paths."""
    out = pathlib.Path(out)
    want = output_files(root)
    for rel in want:
        (root / rel).unlink(missing_ok=True)
    run([sys.executable, str(TOOLS / "screenshots.py"), "--bin", str(sim_bin),
         "--only", ",".join(automated_scenes(root))], check=True, cwd=root)
    run([sys.executable, str(TOOLS / "render-site-variants.py")], check=True, cwd=root)
    run([sys.executable, str(TOOLS / "render-site-glows.py"), "--only", *glow_names()], check=True, cwd=root)
    missing = [rel for rel in want if not (root / rel).is_file()]
    if missing:
        raise Failure(f"the render did not write: {', '.join(missing)}")
    shutil.rmtree(out / "files", ignore_errors=True)
    for rel in want:
        dst = out / "files" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, dst)
    return want


def compare(a, b):
    """{path: (same, size_a, size_b)} for every path of `output_files()` in two render directories."""
    result = {}
    for rel in output_files():
        pa, pb = pathlib.Path(a) / "files" / rel, pathlib.Path(b) / "files" / rel
        if not pa.is_file() or not pb.is_file():
            raise Failure(f"{rel} is missing from {a if not pa.is_file() else b}")
        result[rel] = (pa.read_bytes() == pb.read_bytes(), pa.stat().st_size, pb.stat().st_size)
    return result


def chrome_version(run=subprocess.run):
    for name in ("google-chrome", "chromium", "chromium-browser"):
        exe = shutil.which(name)
        if exe:
            done = run([exe, "--version"], capture_output=True, text=True)
            return done.stdout.strip() or None
    return None


def build_manifest(out, runs, root=ROOT, env=None, rev="HEAD"):
    sv = _site_video()
    out = pathlib.Path(out)
    files = {}
    for rel in output_files(root):
        path = out / "files" / rel
        if not path.is_file():
            raise Failure(f"{rel} is missing from {out}")
        files[rel] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    environment = sv.collect_environment(env)
    environment["chrome"] = chrome_version()
    return {
        "schema": SCHEMA,
        "platform": sv.current_platform(env),
        "tree_hash": tree_hashes(rev=rev, root=root),
        "determinism": {"runs": runs, "byte_identical": True},
        "environment": environment,
        "files": files,
    }


def plan_adopt(artifact, root=ROOT, expect_rev=None):
    """(manifest, changed): verify an artifact directory and list the files whose bytes differ from the
    tree's. Raises Failure for anything it does not trust; touches nothing."""
    artifact = pathlib.Path(artifact)
    try:
        manifest = json.loads((artifact / MANIFEST_NAME).read_text())
    except (OSError, ValueError) as e:
        raise Failure(f"{artifact}: no readable {MANIFEST_NAME}: {e}")
    if manifest.get("schema") != SCHEMA:
        raise Failure(f"refusing: manifest schema {manifest.get('schema')!r}, this tool reads {SCHEMA}")
    if manifest.get("platform") != "linux-ci":
        raise Failure(f"refusing: platform {manifest.get('platform')!r}; only a GitHub Actions Linux render is adoptable")
    det = manifest.get("determinism") or {}
    if det.get("byte_identical") is not True or not isinstance(det.get("runs"), int) or det["runs"] < 2:
        raise Failure("refusing: the render was not proven byte-identical across two runs")
    want = output_files(root)
    recorded = manifest.get("files") or {}
    if sorted(recorded) != want:
        extra, absent = sorted(set(recorded) - set(want)), sorted(set(want) - set(recorded))
        raise Failure(f"refusing: the manifest's file set is not the stills' (extra {extra}, absent {absent})")
    on_disk = sorted(str(p.relative_to(artifact / "files")) for p in (artifact / "files").rglob("*") if p.is_file())
    if on_disk != want:
        raise Failure(f"refusing: the artifact's files are not exactly the stills' ({sorted(set(on_disk) ^ set(want))})")
    changed = []
    for rel in want:
        src = artifact / "files" / rel
        if sha256_file(src) != recorded[rel].get("sha256"):
            raise Failure(f"refusing: {rel} does not match its recorded sha256")
        dst = root / rel
        if not dst.is_file() or dst.read_bytes() != src.read_bytes():
            changed.append(rel)
    if expect_rev:
        want_tree = tree_hashes(root=root, rev=expect_rev)["combined"]
        got = (manifest.get("tree_hash") or {}).get("combined")
        if got != want_tree:
            raise Failure(f"refusing: the artifact was rendered from inputs {str(got)[:12]}, "
                          f"but {expect_rev} has {want_tree[:12]}")
    return manifest, changed


def adopt(artifact, root=ROOT, write=False, say=print, expect_rev=None):
    """Copy the changed files and the manifest into the tree (`write`), or say what would change. Nothing
    changed: nothing written, the manifest included. Returns the changed paths."""
    manifest, changed = plan_adopt(artifact, root, expect_rev)
    if not changed:
        say(f"the {len(manifest['files'])} stills are byte-identical to the committed ones: nothing to adopt")
        return []
    for rel in changed:
        say(f"{'adopt' if write else 'would adopt'} {rel}")
        if write:
            shutil.copyfile(pathlib.Path(artifact) / "files" / rel, root / rel)
    if write:
        (root / MANIFEST_REL).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return changed


def commit_message(tag, run_url, source_sha, film=None, stills=None):
    """The one bot commit that lands what a release re-rendered: the film (`film`, its manifest), the stills
    (`stills`, theirs), or both. At least one."""
    if film is None and stills is None:
        raise Failure("nothing to describe: neither a film nor stills manifest")
    tag = tag or f"main@{source_sha[:8]}"
    what = "demo video and stills" if film is not None and stills is not None else \
        "demo video" if film is not None else "stills"
    paras = []
    if film is not None:
        sv = _site_video()
        body = sv.commit_message(film, tag, run_url, source_sha).split("\n\n", 1)[1]
        paras.append(body.rsplit("\n\nThe gates prove", 1)[0])
    if stills is not None:
        tree = (stills.get("tree_hash") or {}).get("combined", "?")[:12]
        n = len(stills.get("files", {}))
        paras.append(f"The {n} simulator-made stills (documentation figures, the site's close-ups, the link card,\n"
                     f"their WebP and phone copies and glows) were re-rendered by the same run from {tag}\n"
                     f"({source_sha}), inputs tree hash {tree}, on the pinned Linux/llvmpipe stack: two renders\n"
                     f"were byte-identical. Photographs taken on the television are not regenerated.")
    paras.append(f"Run: {run_url}\n\nThe checks prove determinism, not taste. If this looks wrong, revert this commit;\n"
                 f"nothing else depends on it.")
    return f"Site: {what} re-rendered for {tag}\n\n" + "\n\n".join(paras) + "\n"


def cmd_files(args):
    print("\n".join(output_files(manual=args.manual)))


def cmd_render(args):
    paths = render(args.out, args.bin)
    print(f"site_stills: {len(paths)} files in {args.out}/files")


def cmd_compare(args):
    result = compare(args.a, args.b)
    differ = [rel for rel, (same, _, _) in result.items() if not same]
    for rel, (same, sa, sb) in result.items():
        print(f"{'identical' if same else 'DIFFERENT'}  {rel}  {sa} / {sb} bytes")
    print(f"site_stills: {len(result) - len(differ)} of {len(result)} identical")
    return 1 if differ else 0


def cmd_manifest(args):
    manifest = build_manifest(args.dir, args.runs)
    pathlib.Path(args.dir, MANIFEST_NAME).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(f"site_stills: {MANIFEST_NAME}: {len(manifest['files'])} files, tree hash {manifest['tree_hash']['combined'][:12]}")


def cmd_adopt(args):
    adopt(args.dir, write=args.write, expect_rev=args.expect_rev)


def cmd_tree_hash(args):
    print(tree_hashes(rev=args.rev)["combined"])


def cmd_needs_render(args):
    render_it, why = needs_render(args.rev, args.force, args.manifest)
    print(f"needs_stills={'true' if render_it else 'false'}")
    print(f"stills_reason={why}")


def cmd_commit_message(args):
    load = lambda p: json.loads(pathlib.Path(p).read_text()) if p else None  # noqa: E731
    sys.stdout.write(commit_message(args.tag, args.run_url, args.source_sha,
                                    load(args.film_manifest), load(args.stills_manifest)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("files", help="every path the refresh may write (--manual: the ones it leaves to a Mac)")
    p.add_argument("--manual", action="store_true")
    p.set_defaults(fn=cmd_files)
    p = sub.add_parser("render", help="render every still into the checkout, then DIR/files")
    p.add_argument("--out", required=True)
    p.add_argument("--bin", required=True, help="the screenshots simulator (`make screenshots-sim`)")
    p.set_defaults(fn=cmd_render)
    p = sub.add_parser("compare", help="byte-compare two render directories")
    p.add_argument("a")
    p.add_argument("b")
    p.set_defaults(fn=cmd_compare)
    p = sub.add_parser("manifest", help="write DIR/stills.manifest.json")
    p.add_argument("dir")
    p.add_argument("--runs", type=int, default=2)
    p.set_defaults(fn=cmd_manifest)
    p = sub.add_parser("adopt", help="verify an artifact directory and copy it into the tree")
    p.add_argument("dir")
    p.add_argument("--write", action="store_true")
    p.add_argument("--expect-rev")
    p.set_defaults(fn=cmd_adopt)
    p = sub.add_parser("tree-hash")
    p.add_argument("--rev", default="HEAD")
    p.set_defaults(fn=cmd_tree_hash)
    p = sub.add_parser("needs-render")
    p.add_argument("--rev", default="HEAD")
    p.add_argument("--force", action="store_true")
    p.add_argument("--manifest")
    p.set_defaults(fn=cmd_needs_render)
    p = sub.add_parser("commit-message")
    p.add_argument("--tag", default="")
    p.add_argument("--run-url", required=True)
    p.add_argument("--source-sha", required=True)
    p.add_argument("--film-manifest")
    p.add_argument("--stills-manifest")
    p.set_defaults(fn=cmd_commit_message)
    args = ap.parse_args(argv)
    try:
        return args.fn(args) or 0
    except Failure as e:
        print(f"site_stills: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"site_stills: {' '.join(map(str, e.cmd))} exited {e.returncode}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
