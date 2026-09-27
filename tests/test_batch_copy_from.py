"""Tests for the render_batch_copy_from autogenerate option, which embeds
reflected pre-migration table definitions into generated revisions so
that batch ("move and copy") migrations can render full SQL in --sql
mode without a database connection, for both upgrade and downgrade.

"""

import io
import os
import shutil
import sqlite3

import sqlalchemy as sa
from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import Table
from sqlalchemy import text
from sqlalchemy.types import UserDefinedType

from alembic import command
from alembic import util
from alembic.autogenerate import render
from alembic.autogenerate.api import AutogenContext
from alembic.environment import EnvironmentContext
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing.env import _get_staging_directory
from alembic.testing.env import _sqlite_testing_config
from alembic.testing.env import clear_staging_env
from alembic.testing.env import env_file_fixture
from alembic.testing.env import staging_env
from alembic.testing.fixtures import TestBase
from alembic.util import CommandError


def _env_py(extra_configure=""):
    return """
import sqlalchemy as sa
from sqlalchemy import engine_from_config
from sqlalchemy import pool

naming_convention = {
    "uq": "uq_%%(table_name)s_%%(column_0_name)s",
    "ck": "ck_%%(table_name)s_%%(constraint_name)s",
}
target_metadata = sa.MetaData(naming_convention=naming_convention)

foo = sa.Table(
    "foo",
    target_metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("data", sa.Text),
    sa.Column("x", sa.Integer, server_default="0", nullable=False),
    sa.Column("y", sa.String(10)),
    sa.UniqueConstraint("x"),
    sa.CheckConstraint("x >= 0", name="ck_foo_x"),
)
bar = sa.Table(
    "bar",
    target_metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("foo_id", sa.Integer),
    sa.ForeignKeyConstraint(["foo_id"], ["foo.id"], name="fk_bar_foo"),
)
ix_bar_foo_id = sa.Index("ix_bar_foo_id", bar.c.foo_id)


def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table" and name == "extra":
        return False
    return True


def render_item(type_, obj, autogen_context):
    if type_ == "type" and isinstance(obj, sa.Text):
        return "sa.TEXT()"
    return False


def _configure(**kw):
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,
        include_object=include_object,
        render_item=render_item,
        %s
        **kw
    )


def run_migrations_offline():
    _configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
""" % extra_configure


class BatchCopyFromRoundTripTest(TestBase):
    """End to end test: generate a revision against a real SQLite
    database, then render and execute upgrade/downgrade SQL without
    any database connection."""

    def setUp(self):
        self.env = staging_env()
        self.cfg = _sqlite_testing_config()
        self.db_path = os.path.join(
            _get_staging_directory(), "scripts", "foo.db"
        )
        self.work_path = os.path.join(
            _get_staging_directory(), "scripts", "work.db"
        )

    def tearDown(self):
        clear_staging_env()

    def _make_old_db(self):
        engine = sa.create_engine("sqlite:///%s" % self.db_path)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE foo (id INTEGER NOT NULL PRIMARY KEY, "
                    "data VARCHAR(50), x INTEGER DEFAULT 0, "
                    "CONSTRAINT ck_foo_x CHECK (x >= 0))"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE bar (id INTEGER NOT NULL PRIMARY KEY, "
                    "foo_id INTEGER REFERENCES foo(id))"
                )
            )
            conn.execute(text("CREATE UNIQUE INDEX uq_bar_id ON bar (id)"))
            conn.execute(text("CREATE TABLE extra (id INTEGER)"))
            conn.execute(text("INSERT INTO foo VALUES (1, 'some data', 5)"))
            conn.execute(text("INSERT INTO foo VALUES (2, 'other', 9)"))
            conn.execute(text("INSERT INTO bar VALUES (1, 1)"))
            conn.execute(text("INSERT INTO extra VALUES (1)"))
        engine.dispose()

    def _snapshot(self, path):
        engine = sa.create_engine("sqlite:///%s" % path)
        insp = sa.inspect(engine)
        result = {}
        for tname in sorted(insp.get_table_names()):
            if tname == "alembic_version":
                continue
            cols = [
                (
                    c["name"],
                    str(c["type"]),
                    c["nullable"],
                    str(c["default"]) if c["default"] is not None else None,
                )
                for c in insp.get_columns(tname)
            ]
            pk = tuple(insp.get_pk_constraint(tname)["constrained_columns"])
            uniques = sorted(
                (u["name"], tuple(u["column_names"]))
                for u in insp.get_unique_constraints(tname)
            )
            checks = sorted(
                (c["name"], c["sqltext"])
                for c in insp.get_check_constraints(tname)
            )
            fks = sorted(
                (
                    f["name"],
                    tuple(f["constrained_columns"]),
                    f["referred_table"],
                    tuple(f["referred_columns"]),
                )
                for f in insp.get_foreign_keys(tname)
            )
            idxs = sorted(
                (i["name"], tuple(i["column_names"]), bool(i["unique"]))
                for i in insp.get_indexes(tname)
            )
            result[tname] = (cols, pk, uniques, checks, fks, idxs)
        engine.dispose()
        return result

    def _rows(self, path, table):
        db = sqlite3.connect(path)
        try:
            return db.execute("select * from %s" % table).fetchall()
        finally:
            db.close()

    def _exec_sql(self, path, sql):
        db = sqlite3.connect(path)
        try:
            db.executescript(sql)
        finally:
            db.close()

    def _offline_sql(self, direction, revision):
        buf = io.StringIO()
        self.cfg.output_buffer = buf
        getattr(command, direction)(self.cfg, revision, sql=True)
        return buf.getvalue()

    def test_off_by_default(self):
        env_file_fixture(_env_py())
        self._make_old_db()

        rev = command.revision(self.cfg, message="test", autogenerate=True)
        with open(rev.path) as f:
            source = f.read()
        assert "batch_alter_table" in source
        assert "copy_from" not in source

    def test_requires_render_as_batch(self):
        env_ctx = EnvironmentContext(
            self.cfg, ScriptDirectory.from_config(self.cfg)
        )
        assert_raises_message(
            CommandError,
            "render_batch_copy_from requires render_as_batch=True",
            env_ctx.configure,
            render_batch_copy_from=True,
        )

    def test_roundtrip(self):
        env_file_fixture(_env_py("render_batch_copy_from=True,"))
        self._make_old_db()

        # one online autogenerate produces the revision
        rev = command.revision(self.cfg, message="test", autogenerate=True)

        # the revision command creates the (empty) version table as a
        # side effect; remove it so the database is back to its
        # pre-migration state
        self._exec_sql(self.db_path, "DROP TABLE alembic_version;")
        orig_snapshot = self._snapshot(self.db_path)

        with open(rev.path) as f:
            source = f.read()

        # the pre-migration definition of each batch table is embedded
        # and passed as copy_from, in both directions
        eq_(source.count("copy_from=_alembic_batch_copy_from_foo"), 2)
        eq_(source.count("copy_from=_alembic_batch_copy_from_bar"), 2)
        assert "sa.Table(" in source
        # pre-upgrade structure, not the target structure
        assert "sa.Column('data', sa.VARCHAR(length=50)" in source
        # post-upgrade structure embedded for the downgrade
        assert "sa.Column('data', sa.TEXT()" in source
        # naming_convention, include_object and render_item hooks
        assert "uq_foo_x" in source
        assert "sa.CheckConstraint('x >= 0', name='ck_foo_x')" in source
        assert "extra" not in source

        # the file imports standalone, without the generation-time
        # connection or in-memory objects
        module = util.load_python_file(
            os.path.dirname(rev.path), os.path.basename(rev.path)
        )
        assert callable(module.upgrade)
        assert callable(module.downgrade)

        # keep a copy of the old database, then remove the original;
        # everything below happens without it
        os.rename(self.db_path, self.work_path)
        shutil.copy(self.work_path, self.db_path + ".orig")

        # upgrade SQL, rendered offline
        up_sql = self._offline_sql("upgrade", "head")
        assert "CREATE TABLE _alembic_tmp_foo" in up_sql
        assert "CONSTRAINT uq_foo_x UNIQUE (x)" in up_sql
        assert "CONSTRAINT ck_foo_x CHECK (x >= 0)" in up_sql

        self._exec_sql(self.work_path, up_sql)

        eq_(
            self._rows(self.work_path, "foo"),
            [(1, "some data", 5, None), (2, "other", 9, None)],
        )
        eq_(self._rows(self.work_path, "bar"), [(1, 1)])
        eq_(self._rows(self.work_path, "extra"), [(1,)])

        up_snapshot = self._snapshot(self.work_path)
        foo_cols = {c[0]: c for c in up_snapshot["foo"][0]}
        eq_(foo_cols["data"][1], "TEXT")
        eq_(foo_cols["x"][2], False)
        assert "y" in foo_cols
        eq_(up_snapshot["foo"][2], [("uq_foo_x", ("x",))])
        eq_(up_snapshot["foo"][3], [("ck_foo_x", "x >= 0")])
        eq_(
            [idx for idx in up_snapshot["bar"][5]],
            [("ix_bar_foo_id", ("foo_id",), False)],
        )

        # downgrade SQL, also rendered offline, run on top of the
        # upgraded database
        down_sql = self._offline_sql("downgrade", "head:base")
        self._exec_sql(self.work_path, down_sql)

        eq_(
            self._rows(self.work_path, "foo"),
            [(1, "some data", 5), (2, "other", 9)],
        )
        eq_(self._rows(self.work_path, "bar"), [(1, 1)])

        # the schema is back to the original structure
        eq_(self._snapshot(self.work_path), orig_snapshot)

        # the same revision also runs online against a live database
        shutil.copy(self.db_path + ".orig", self.db_path)
        command.upgrade(self.cfg, "head")
        eq_(self._snapshot(self.db_path), up_snapshot)
        command.downgrade(self.cfg, "base")
        eq_(self._snapshot(self.db_path), orig_snapshot)


class MyType(UserDefinedType):
    def get_col_spec(self):
        return "MYTYPE"


class MyConstraint(sa.Constraint):
    pass


class BatchCopyFromRenderTest(TestBase):
    """Unit tests for rendering of embedded copy_from tables."""

    def _autogen_context(self, **opts):
        ctx_opts = {
            "sqlalchemy_module_prefix": "sa.",
            "alembic_module_prefix": "op.",
            "user_module_prefix": None,
            "target_metadata": MetaData(),
        }
        ctx_opts.update(opts)
        from sqlalchemy.engine.default import DefaultDialect

        context = MigrationContext.configure(
            dialect=DefaultDialect(), opts=ctx_opts
        )
        return AutogenContext(context)

    def _table(self, *args):
        m = MetaData()
        return Table("t", m, Column("id", Integer, primary_key=True), *args)

    def test_unrenderable_type_refused(self):
        table = self._table(Column("x", MyType()))
        ac = self._autogen_context()
        assert_raises_message(
            CommandError,
            "Can't embed a batch copy_from table definition: " "column type",
            render._render_copy_from_assignment,
            "v",
            table,
            [],
            ac,
        )

    def test_unrenderable_type_allowed_with_user_module_prefix(self):
        table = self._table(Column("x", MyType()))
        ac = self._autogen_context(user_module_prefix="mytypes.")
        out = render._render_copy_from_assignment("v", table, [], ac)
        assert "mytypes.MyType()" in out

    def test_unrenderable_type_allowed_with_render_item(self):
        def render_item(type_, obj, autogen_context):
            if type_ == "type" and isinstance(obj, MyType):
                return "sa.Text()"
            return False

        table = self._table(Column("x", MyType()))
        ac = self._autogen_context(render_item=render_item)
        out = render._render_copy_from_assignment("v", table, [], ac)
        assert "sa.Text()" in out

    def test_unknown_constraint_refused(self):
        table = self._table()
        table.append_constraint(MyConstraint(name="mc"))
        ac = self._autogen_context()
        assert_raises_message(
            CommandError,
            "Can't embed a batch copy_from table definition: "
            "no renderer is established for object",
            render._render_copy_from_assignment,
            "v",
            table,
            [],
            ac,
        )

    def test_table_elements_rendered(self):
        m = MetaData()
        Table("other", m, Column("oid", Integer, primary_key=True))
        table = Table(
            "t",
            m,
            Column("id", Integer, primary_key=True),
            Column("data", sa.String(50), server_default="hi"),
            Column("oid", Integer),
            sa.UniqueConstraint("data", name="uq_t_data"),
            sa.CheckConstraint("id > 0", name="ck_t_id"),
            sa.ForeignKeyConstraint(["oid"], ["other.oid"], name="fk_t_o"),
            sa.Index("ix_t_oid", "oid"),
        )
        ac = self._autogen_context()
        out = render._render_copy_from_assignment(
            "v", table, list(table.indexes), ac
        )
        assert out.startswith("v = sa.Table(")
        assert "'t'" in out
        assert "sa.MetaData()" in out
        assert "sa.Column('id', sa.Integer(), nullable=False)" in out
        assert "server_default='hi'" in out
        assert "sa.PrimaryKeyConstraint('id')" in out
        assert "sa.UniqueConstraint('data', name='uq_t_data')" in out
        assert "sa.CheckConstraint('id > 0', name='ck_t_id')" in out
        assert (
            "sa.ForeignKeyConstraint(['oid'], ['other.oid'], "
            "name='fk_t_o')" in out
        )
        assert "sa.Index('ix_t_oid', 'oid')" in out
