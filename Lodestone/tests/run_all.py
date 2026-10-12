#!/usr/bin/env python3
"""Run every Lodestone self-test.  py tests/run_all.py"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = ["test_http_and_api.py", "test_files_props_discord_backups.py",
         "test_rcon_paths_multiworld.py", "test_startup.py",
         "test_assistant_and_migration.py", "test_chat_tools_local_ai.py",
         "test_minehut_worlds_icon.py", "test_sftp.py", "test_networking.py",
         "test_multiserver.py", "test_via.py", "test_creator.py", "test_textures.py", "test_updater.py", "test_ram.py",
         "test_ollama.py", "test_devtools.py",
         "test_page_syntax.py"]

def main():
    bad = []
    for t in TESTS:
        print("\n=== %s" % t)
        r = subprocess.run([sys.executable, os.path.join(HERE, t)])
        if r.returncode != 0:
            bad.append(t)
    print("\n" + ("all suites passed" if not bad else "FAILED: " + ", ".join(bad)))
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
