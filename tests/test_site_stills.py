"""tools/site_stills.py: which stills the refresh owns, and the adopt step's refusals.

Offline: no simulator, no ffmpeg. Artifacts are built in a private temp tree that stands in for the repository.
"""
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("site_stills", ROOT / "tools" / "site_stills.py")
ss = importlib.util.module_from_spec(_spec)
sys.modules["site_stills"] = ss
_spec.loader.exec_module(ss)


def tracked(prefix):
    out = subprocess.run(["git", "ls-files", prefix], capture_output=True, text=True, cwd=ROOT, check=True).stdout
    return set(out.split())


class FileSet(unittest.TestCase):
    def test_automated_and_manual_are_disjoint_and_cover_every_simulator_made_still(self):
        auto, manual = set(ss.output_files()), set(ss.output_files(manual=True))
        self.assertFalse(auto & manual)
        committed = tracked("docs/screenshots") | tracked("site/media")
        film = {p for p in committed if p.startswith("site/media/feel")}
        # what is committed and in neither set is the film and the photograph taken on the television, nothing else
        self.assertEqual(committed - auto - manual - film, {"docs/screenshots/navblur-transition.jpg"})
        self.assertLessEqual(auto | manual, committed)

    def test_the_film_and_the_television_photograph_are_never_in_a_set(self):
        for p in ss.output_files() + ss.output_files(manual=True):
            self.assertNotIn("feel", p.split("/")[-1])
            self.assertNotIn("navblur", p)

    def test_a_manual_scene_takes_everything_cut_from_it(self):
        manual = ss.output_files(manual=True)
        self.assertIn("site/media/closeup-player-glow.png", manual)
        self.assertIn("site/media/closeup-player-narrow.webp", manual)
        self.assertIn("site/media/og-card.jpg", manual)  # composed around the `home` figure
        self.assertNotIn("site/media/closeup-glass.jpg", manual)

    def test_every_manual_scene_exists_in_the_scene_manifest(self):
        self.assertLessEqual(set(ss.MANUAL_SCENES), {s["name"] for s in ss.scene_table()})
        self.assertEqual(set(ss.automated_scenes()) | set(ss.MANUAL_SCENES), {s["name"] for s in ss.scene_table()})


class Adopt(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo, self.art = self.tmp / "repo", self.tmp / "art"
        for rel in ss.output_files():
            for base, body in ((self.repo, b"old " + rel.encode()), (self.art / "files", b"new " + rel.encode())):
                (base / rel).parent.mkdir(parents=True, exist_ok=True)
                (base / rel).write_bytes(body)
        # `plan_adopt` reads the scene manifest and the variant tools from `root`
        for rel in ("tests/screenshots/scenes.json",):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, self.repo / rel)
        self.write_manifest()

    def write_manifest(self, **over):
        m = {"schema": 1, "platform": "linux-ci", "tree_hash": {"combined": "x"},
             "determinism": {"runs": 2, "byte_identical": True},
             "files": {rel: {"sha256": ss.sha256_file(self.art / "files" / rel)} for rel in ss.output_files()}}
        m.update(over)
        (self.art / ss.MANIFEST_NAME).write_text(json.dumps(m))

    def adopt(self, write=False):
        return ss.adopt(self.art, root=self.repo, write=write, say=lambda *_: None)

    def test_a_dry_run_lists_and_writes_nothing(self):
        self.assertEqual(sorted(self.adopt()), ss.output_files())
        self.assertFalse((self.repo / ss.MANIFEST_REL).exists())
        self.assertEqual((self.repo / "site/media/closeup-glass.jpg").read_bytes(), b"old site/media/closeup-glass.jpg")

    def test_write_copies_the_changed_files_and_the_manifest(self):
        self.adopt(write=True)
        self.assertEqual((self.repo / "site/media/closeup-glass.jpg").read_bytes(), b"new site/media/closeup-glass.jpg")
        self.assertTrue((self.repo / ss.MANIFEST_REL).is_file())

    def test_identical_stills_write_nothing_not_even_the_manifest(self):
        for rel in ss.output_files():
            shutil.copyfile(self.art / "files" / rel, self.repo / rel)
        self.assertEqual(self.adopt(write=True), [])
        self.assertFalse((self.repo / ss.MANIFEST_REL).exists())

    def test_refusals(self):
        for over, why in (({"platform": "darwin-local"}, "platform"),
                          ({"determinism": {"runs": 1, "byte_identical": True}}, "byte-identical"),
                          ({"determinism": {"runs": 2, "byte_identical": False}}, "byte-identical"),
                          ({"schema": 2}, "schema")):
            self.write_manifest(**over)
            with self.assertRaisesRegex(ss.Failure, why):
                self.adopt()

    def test_a_tampered_file_an_extra_file_and_a_missing_file_are_refused(self):
        self.write_manifest()
        victim = self.art / "files" / "site/media/closeup-glass.jpg"
        victim.write_bytes(b"tampered")
        with self.assertRaisesRegex(ss.Failure, "sha256"):
            self.adopt()
        self.write_manifest()
        (self.art / "files" / "site" / "media" / "feel-poster.jpg").write_bytes(b"x")
        with self.assertRaisesRegex(ss.Failure, "exactly"):
            self.adopt()
        (self.art / "files" / "site" / "media" / "feel-poster.jpg").unlink()
        victim.unlink()
        with self.assertRaises(ss.Failure):
            self.adopt()

    def test_a_manifest_naming_another_file_set_is_refused(self):
        m = json.loads((self.art / ss.MANIFEST_NAME).read_text())
        m["files"]["docs/screenshots/navblur-transition.jpg"] = {"sha256": "0"}
        (self.art / ss.MANIFEST_NAME).write_text(json.dumps(m))
        with self.assertRaisesRegex(ss.Failure, "file set"):
            self.adopt()


class Decide(unittest.TestCase):
    def test_forced_and_unreadable_manifests_render_and_a_matching_tree_does_not(self):
        self.assertTrue(ss.needs_render(force=True)[0])
        self.assertTrue(ss.needs_render(manifest_path="/nonexistent/m.json")[0])
        with tempfile.TemporaryDirectory() as d:
            m = pathlib.Path(d, "m.json")
            m.write_text(json.dumps({"tree_hash": ss.tree_hashes()}))
            self.assertFalse(ss.needs_render(manifest_path=m)[0])

    def test_commit_message_for_each_combination_and_for_none(self):
        stills = {"tree_hash": {"combined": "a" * 64}, "files": {"a": {}, "b": {}}}
        msg = ss.commit_message("v1.2.3", "https://run", "f" * 40, stills=stills)
        self.assertTrue(msg.startswith("Site: stills re-rendered for v1.2.3\n"))
        self.assertIn("revert this commit", msg)
        self.assertTrue(ss.commit_message("", "u", "f" * 40, stills=stills).startswith("Site: stills re-rendered for main@ffffffff"))
        with self.assertRaises(ss.Failure):
            ss.commit_message("v1.2.3", "u", "s")


if __name__ == "__main__":
    unittest.main()
