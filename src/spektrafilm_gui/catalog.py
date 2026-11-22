"""Local photo catalogue. Original image files are only stat'ed, never modified."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from os import stat_result
from pathlib import Path
import sqlite3
from typing import Iterable

RAW_EXTENSIONS = frozenset({'.3fr', '.arw', '.cr2', '.cr3', '.crw', '.dcr', '.dng', '.erf', '.fff', '.iiq', '.kdc', '.mef', '.mos', '.mrw', '.nef', '.nrw', '.orf', '.pef', '.raf', '.raw', '.rw2', '.rwl', '.sr2', '.srf', '.srw', '.x3f'})
SUPPORTED_EXTENSIONS = RAW_EXTENSIONS | {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.exr'}
COLORS = frozenset({'', 'red', 'yellow', 'green', 'blue', 'purple'})
SCHEMA_VERSION = 1
APPLICATION_ID = 0x53504643


@dataclass(frozen=True)
class Photo:
    id: int
    path: str
    name: str
    size: int
    mtime_ns: int
    rating: int
    color: str
    flag: int
    tags: tuple[str, ...]


def _image_path(path: str | Path) -> tuple[Path, stat_result]:
    result = Path(path).expanduser().resolve(strict=True)
    if not result.is_file() or result.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f'Unsupported image file: {result}')
    return result, result.stat()


class Catalog:
    def __init__(self, database_path: str | Path):
        self.path = Path(database_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            app_id = db.execute('PRAGMA application_id').fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version > SCHEMA_VERSION:
                raise ValueError('This catalogue was created by a newer version of Spektrafilm.')
            if app_id not in (0, APPLICATION_ID) or (tables and app_id != APPLICATION_ID):
                raise ValueError('The selected database is not a Spektrafilm catalogue.')
            if version == 0:
                db.execute('''CREATE TABLE photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE,
                    folder TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL, rating INTEGER NOT NULL DEFAULT 0 CHECK(rating BETWEEN 0 AND 5),
                    color TEXT NOT NULL DEFAULT '' CHECK(color IN ('','red','yellow','green','blue','purple')),
                    flag INTEGER NOT NULL DEFAULT 0 CHECK(flag IN (-1,0,1)))''')
                db.execute('CREATE INDEX photos_folder ON photos(folder)')
                db.execute('CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL, key TEXT NOT NULL UNIQUE)')
                db.execute('''CREATE TABLE photo_tags (
                    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    PRIMARY KEY(photo_id, tag_id))''')
                db.execute('''CREATE TABLE photo_edits (
                    photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
                    state TEXT NOT NULL, engine_version TEXT NOT NULL, format_version INTEGER NOT NULL DEFAULT 1)''')
                db.execute(f'PRAGMA application_id = {APPLICATION_ID}')
                db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        with self._connection() as db:
            db.execute('PRAGMA journal_mode = WAL')

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        db.create_function('casefold', 1, lambda value: value.casefold())
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_files(self, paths: Iterable[str | Path]) -> tuple[int, int]:
        added = existing = 0
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for raw in paths:
                path, stat = _image_path(raw)
                old = db.execute('SELECT id FROM photos WHERE path = ?', (str(path),)).fetchone()
                db.execute('''INSERT INTO photos(path,folder,name,size,mtime_ns) VALUES(?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns''',
                    (str(path), str(path.parent), path.name, stat.st_size, stat.st_mtime_ns))
                if old is None:
                    added += 1
                else:
                    existing += 1
        return added, existing

    @staticmethod
    def _photo(db, row) -> Photo:
        tags = db.execute('''SELECT tags.name FROM tags JOIN photo_tags ON tags.id=photo_tags.tag_id
            WHERE photo_id=? ORDER BY tags.key''', (row['id'],)).fetchall()
        return Photo(*(row[key] for key in ('id', 'path', 'name', 'size', 'mtime_ns', 'rating', 'color', 'flag')),
                     tuple(tag[0] for tag in tags))

    def get_photo(self, photo_id: int) -> Photo | None:
        with self._connection() as db:
            row = db.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
            return self._photo(db, row) if row is not None else None

    def get_by_path(self, path: str | Path) -> Photo | None:
        normalized = str(Path(path).expanduser().resolve())
        with self._connection() as db:
            row = db.execute('SELECT * FROM photos WHERE path=?', (normalized,)).fetchone()
            return self._photo(db, row) if row is not None else None

    @staticmethod
    def _filters(search='', min_rating=0, color='', flag=None, folder=''):
        if type(min_rating) is not int or not 0 <= min_rating <= 5:
            raise ValueError('Rating must be an integer from 0 to 5.')
        if not isinstance(color, str) or color not in COLORS or (flag is not None and (type(flag) is not int or flag not in (-1, 0, 1))):
            raise ValueError('Invalid colour or pick flag.')
        clauses, args = ['rating >= ?'], [min_rating]
        if color:
            clauses.append('color=?'); args.append(color)
        if flag is not None:
            clauses.append('flag=?'); args.append(flag)
        if folder:
            clauses.append('folder=?'); args.append(str(Path(folder).expanduser().resolve()))
        if search:
            # ponytail: substring search scans rows; use FTS if very large catalogues need faster search.
            clauses.append('''(instr(casefold(path),?) > 0 OR EXISTS (
                SELECT 1 FROM photo_tags JOIN tags ON tags.id=photo_tags.tag_id
                WHERE photo_tags.photo_id=photos.id AND instr(tags.key,?) > 0))''')
            args.extend([search.casefold(), search.casefold()])
        return ' AND '.join(clauses), args

    def list_photos(self, *, search='', min_rating=0, color='', flag=None, folder='', limit=200, offset=0) -> list[Photo]:
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise ValueError('Invalid page size or offset.')
        where, args = self._filters(search, min_rating, color, flag, folder)
        with self._connection() as db:
            rows = db.execute(f'SELECT * FROM photos WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?',
                              (*args, limit, offset)).fetchall()
            return [self._photo(db, row) for row in rows]

    def count_photos(self, *, search='', min_rating=0, color='', flag=None, folder='') -> int:
        where, args = self._filters(search, min_rating, color, flag, folder)
        with self._connection() as db:
            return db.execute(f'SELECT count(*) FROM photos WHERE {where}', args).fetchone()[0]

    def folders(self) -> list[str]:
        with self._connection() as db:
            return [row[0] for row in db.execute('SELECT DISTINCT folder FROM photos ORDER BY folder')]

    @staticmethod
    def _require_photo(db, photo_id):
        if db.execute('SELECT 1 FROM photos WHERE id=?', (photo_id,)).fetchone() is None:
            raise ValueError(f'Photo not found: {photo_id}')

    def update_photo(self, photo_id: int, *, rating=None, color=None, flag=None, tags=None) -> None:
        if rating is not None and (type(rating) is not int or not 0 <= rating <= 5):
            raise ValueError('Rating must be an integer from 0 to 5.')
        if color is not None and (not isinstance(color, str) or color not in COLORS):
            raise ValueError('Invalid colour.')
        if flag is not None and (type(flag) is not int or flag not in (-1, 0, 1)):
            raise ValueError('Invalid pick flag.')
        if tags is not None:
            if isinstance(tags, str):
                raise ValueError('Tags must be a sequence of strings.')
            tags = tuple(tags)
            if any(not isinstance(tag, str) or len(tag.strip()) > 200 for tag in tags):
                raise ValueError('Tags must be strings of at most 200 characters.')
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._require_photo(db, photo_id)
            for column, value in (('rating', rating), ('color', color), ('flag', flag)):
                if value is not None:
                    db.execute(f'UPDATE photos SET {column}=? WHERE id=?', (value, photo_id))
            if tags is not None:
                db.execute('DELETE FROM photo_tags WHERE photo_id=?', (photo_id,))
                for tag in tags:
                    name = tag.strip()
                    if not name:
                        continue
                    db.execute('INSERT INTO tags(name,key) VALUES(?,?) ON CONFLICT(key) DO NOTHING', (name, name.casefold()))
                    db.execute('''INSERT OR IGNORE INTO photo_tags(photo_id,tag_id)
                        SELECT ?,id FROM tags WHERE key=?''', (photo_id, name.casefold()))

    def remove_photo(self, photo_id: int) -> None:
        with self._connection() as db:
            db.execute('DELETE FROM photos WHERE id=?', (photo_id,))

    def relocate_photo(self, photo_id: int, new_path: str | Path) -> None:
        path, stat = _image_path(new_path)
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._require_photo(db, photo_id)
            other = db.execute('SELECT id FROM photos WHERE path=? AND id<>?', (str(path), photo_id)).fetchone()
            if other is not None:
                raise ValueError('That file is already in the catalogue.')
            db.execute('UPDATE photos SET path=?,folder=?,name=?,size=?,mtime_ns=? WHERE id=?',
                       (str(path), str(path.parent), path.name, stat.st_size, stat.st_mtime_ns, photo_id))

    def save_edit(self, photo_id: int, state: dict, engine_version: str) -> None:
        if not isinstance(state, dict) or not isinstance(engine_version, str):
            raise ValueError('An edit requires a state object and an engine version.')
        encoded = json.dumps(state, ensure_ascii=False, allow_nan=False)
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._require_photo(db, photo_id)
            previous = db.execute('SELECT format_version FROM photo_edits WHERE photo_id=?', (photo_id,)).fetchone()
            if previous is not None and previous[0] != 1:
                raise ValueError('Unsupported photo edit format; existing edit was not overwritten.')
            db.execute('''INSERT INTO photo_edits(photo_id,state,engine_version) VALUES(?,?,?)
                ON CONFLICT(photo_id) DO UPDATE SET state=excluded.state,engine_version=excluded.engine_version''',
                (photo_id, encoded, engine_version))

    def get_edit(self, photo_id: int) -> dict | None:
        with self._connection() as db:
            row = db.execute('SELECT state,format_version FROM photo_edits WHERE photo_id=?', (photo_id,)).fetchone()
            if row is None:
                return None
            if row['format_version'] != 1:
                raise ValueError('Unsupported photo edit format.')
            state = json.loads(row['state'])
            if not isinstance(state, dict):
                raise ValueError('Invalid photo edit state.')
            return state
