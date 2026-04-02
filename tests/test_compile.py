#!/usr/bin/env python3
"""Test 1 : Compilation de tous les modules Python du projet."""

import os
import sys
import glob
import py_compile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


class TestCompilation(unittest.TestCase):
    """Vérifie que tous les fichiers .py compilent sans erreur de syntaxe."""

    def _compile_file(self, filepath):
        """Helper : compile un fichier et retourne l'erreur éventuelle."""
        try:
            py_compile.compile(filepath, doraise=True)
            return None
        except py_compile.PyCompileError as e:
            return str(e)

    def test_biblio_py(self):
        """biblio.py compile sans erreur."""
        err = self._compile_file(os.path.join(PROJECT_ROOT, 'biblio.py'))
        self.assertIsNone(err, err)

    def test_lib_modules(self):
        """Tous les modules lib/*.py compilent sans erreur."""
        lib_dir = os.path.join(PROJECT_ROOT, 'lib')
        modules = glob.glob(os.path.join(lib_dir, '*.py'))
        self.assertGreater(len(modules), 0, "Aucun module trouvé dans lib/")
        for mod in sorted(modules):
            with self.subTest(module=os.path.basename(mod)):
                err = self._compile_file(mod)
                self.assertIsNone(err, err)

    def test_organiser_modules(self):
        """Les modules organiser/*.py compilent (legacy)."""
        org_dir = os.path.join(PROJECT_ROOT, 'organiser')
        modules = glob.glob(os.path.join(org_dir, '*.py'))
        for mod in sorted(modules):
            with self.subTest(module=os.path.basename(mod)):
                err = self._compile_file(mod)
                self.assertIsNone(err, err)

    def test_renommage_modules(self):
        """Les modules renommage/*.py compilent."""
        ren_dir = os.path.join(PROJECT_ROOT, 'renommage')
        modules = glob.glob(os.path.join(ren_dir, '*.py'))
        for mod in sorted(modules):
            with self.subTest(module=os.path.basename(mod)):
                err = self._compile_file(mod)
                self.assertIsNone(err, err)


if __name__ == '__main__':
    unittest.main()
