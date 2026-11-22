"""Run directly with python tests/test_catalog.py; no GUI/dependencies required."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from spektrafilm_gui.catalog import APPLICATION_ID, Catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / '旅行照片'
        self.source.mkdir()
        self.a = self.source / '海边.JPG'
        self.b = self.source / '人像.dng'
        self.original = b'original-image-bytes-are-never-rewritten'
        self.a.write_bytes(self.original)
        self.b.write_bytes(self.original + b'b')
        self.db = Catalog(self.root / 'catalog.sqlite3')

    def add_photos(self):
        self.db.import_files([self.a, self.b])
        return {p.name: p for p in self.db.list_photos()}

    def test_reimport_alias_filters_and_validation(self):
        self.assertEqual(self.db.import_files([self.a, self.b]), (2, 0))
        alias = self.root / 'alias.jpg'
        alias.symlink_to(self.a)
        self.assertEqual(self.db.import_files([alias, self.a]), (0, 2))
        self.assertEqual(self.db.get_by_path(alias), self.db.get_by_path(self.a))
        self.assertIsNone(self.db.get_by_path(self.root / 'not-imported.jpg'))
        a = next(p for p in self.db.list_photos() if p.name == self.a.name)
        self.db.update_photo(a.id, rating=5, flag=1, color='green', tags=[' 海边 ', '旅行', '海边', 'Straße'])
        self.db.import_files([self.a])
        photo = self.db.get_photo(a.id)
        self.assertEqual((photo.rating, photo.flag, photo.color), (5, 1, 'green'))
        self.assertEqual(set(photo.tags), {'海边', '旅行', 'Straße'})
        self.assertEqual(self.db.count_photos(search='STRASSE', min_rating=4, color='green', flag=1), 1)
        self.assertEqual(self.db.count_photos(search='海边', folder=self.source), 1)
        self.assertEqual(self.db.count_photos(search="%' OR 1=1--"), 0)
        self.assertEqual(self.db.folders(), [str(self.source)])
        for kwargs in ({'rating': 6}, {'rating': True}, {'flag': 2}, {'color': 'ultraviolet'}, {'tags': 'not-a-list'}):
            with self.assertRaises(ValueError):
                self.db.update_photo(a.id, **kwargs)
        with self.assertRaises(ValueError):
            self.db.list_photos(limit=-1)
        self.assertEqual(self.a.read_bytes(), self.original)

    def test_import_rolls_back_on_invalid_file_and_iterator_error(self):
        with self.assertRaises(FileNotFoundError):
            self.db.import_files([self.a, self.source / 'missing.jpg'])
        self.assertEqual(self.db.count_photos(), 0)
        unsupported = self.source / 'notes.txt'
        unsupported.write_text('keep me')
        with self.assertRaises(ValueError):
            self.db.import_files([unsupported])
        def interrupted():
            yield self.a
            raise RuntimeError('import interrupted')
        with self.assertRaises(RuntimeError):
            self.db.import_files(interrupted())
        self.assertEqual(self.db.count_photos(), 0)
        self.assertEqual(self.a.read_bytes(), self.original)

    def test_deleted_photo_id_is_not_reused_by_another_instance(self):
        self.db.import_files([self.a])
        old_id = self.db.get_by_path(self.a).id
        other = Catalog(self.db.path)
        other.remove_photo(old_id)
        other.import_files([self.b])
        self.assertNotEqual(other.get_by_path(self.b).id, old_id)
        with self.assertRaises(ValueError):
            self.db.save_edit(old_id, {'exposure': 9}, '0.3.4')
        self.assertIsNone(other.get_edit(other.get_by_path(self.b).id))

    def test_missing_relocate_edits_and_remove_never_delete_original(self):
        photos = self.add_photos()
        a, b = photos[self.a.name], photos[self.b.name]
        self.db.update_photo(a.id, rating=4, tags=['保留'])
        self.db.save_edit(a.id, {'exposure': 1, 'preset': '柔和'}, '0.3.4')
        self.db.save_edit(b.id, {'exposure': -2}, '0.3.4')
        relocated = self.root / '新位置.jpg'
        self.a.rename(relocated)
        reopened = Catalog(self.db.path)
        self.assertEqual(reopened.count_photos(), 2)
        self.assertFalse(Path(reopened.get_photo(a.id).path).exists())
        with self.assertRaises(ValueError):
            reopened.relocate_photo(a.id, self.b)
        reopened.relocate_photo(a.id, relocated)
        self.assertEqual(reopened.get_photo(a.id).rating, 4)
        self.assertEqual(reopened.get_photo(a.id).tags, ('保留',))
        self.assertEqual(reopened.get_edit(a.id), {'exposure': 1, 'preset': '柔和'})
        self.assertEqual(reopened.get_edit(b.id), {'exposure': -2})
        reopened.remove_photo(a.id)
        self.assertIsNone(reopened.get_photo(a.id))
        self.assertIsNone(reopened.get_edit(a.id))
        self.assertEqual(relocated.read_bytes(), self.original)
        self.assertEqual(self.b.read_bytes(), self.original + b'b')
        with self.assertRaises(ValueError):
            reopened.save_edit(b.id, {'exposure': float('nan')}, '0.3.4')
        self.assertEqual(reopened.get_edit(b.id), {'exposure': -2})
        with closing(sqlite3.connect(self.db.path)) as conn, conn:
            conn.execute('UPDATE photo_edits SET format_version=2 WHERE photo_id=?', (b.id,))
        with self.assertRaises(ValueError):
            reopened.save_edit(b.id, {'exposure': 9}, '0.3.4')
        with closing(sqlite3.connect(self.db.path)) as conn, conn:
            self.assertIn('-2', conn.execute('SELECT state FROM photo_edits WHERE photo_id=?', (b.id,)).fetchone()[0])

    def test_unknown_newer_and_unrelated_databases_are_rejected(self):
        with closing(sqlite3.connect(self.db.path)) as conn, conn:
            conn.execute('PRAGMA user_version=99')
        with self.assertRaises(ValueError):
            Catalog(self.db.path)
        unrelated = self.root / 'other.sqlite3'
        with closing(sqlite3.connect(unrelated)) as conn, conn:
            conn.execute('CREATE TABLE user_data (name TEXT)')
            conn.execute("INSERT INTO user_data VALUES ('untouched')")
        with self.assertRaises(ValueError):
            Catalog(unrelated)
        with closing(sqlite3.connect(unrelated)) as conn, conn:
            self.assertEqual(conn.execute('SELECT name FROM user_data').fetchone()[0], 'untouched')
            self.assertEqual(conn.execute('PRAGMA application_id').fetchone()[0], 0)

    def test_separate_thread_connections_and_pagination(self):
        def add(path):
            return Catalog(self.db.path).import_files([path])
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(add, [self.a, self.b, self.a]))
        self.assertEqual(self.db.count_photos(), 2)
        first = self.db.list_photos(limit=1)[0]
        second = self.db.list_photos(limit=1, offset=1)[0]
        self.assertNotEqual(first.id, second.id)
        with closing(sqlite3.connect(self.db.path)) as conn, conn:
            self.assertEqual(conn.execute('PRAGMA application_id').fetchone()[0], APPLICATION_ID)
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')


if __name__ == '__main__':
    unittest.main()
