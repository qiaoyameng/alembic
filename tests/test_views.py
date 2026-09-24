from __future__ import annotations

import io

from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import text

from alembic import util
from alembic.autogenerate import produce_migrations
from alembic.autogenerate import render_python_code
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations.ops import CreateViewOp
from alembic.operations.ops import DropViewOp
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing import TestBase
from alembic.views import ManagedViews
from alembic.views import normalize_view_select
from alembic.views import RemovedView
from alembic.views import ViewDefinition


def _model_metadata():
    metadata = MetaData()
    Table(
        "base",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("data", String),
    )
    return metadata


class ViewFixture:
    def _connect(self):
        eng = create_engine("sqlite://")
        conn = eng.connect()
        conn.execute(
            text(
                "CREATE TABLE base (id INTEGER PRIMARY KEY, data VARCHAR)"
            )
        )
        conn.execute(text("INSERT INTO base VALUES (1, 'x'), (2, 'y')"))
        conn.commit()
        self.connections.append(conn)
        return conn

    def setup_method(self):
        self.connections = []

    def teardown_method(self):
        for conn in self.connections:
            conn.close()

    def _autogen(self, conn, managed_views):
        metadata = _model_metadata()
        context = MigrationContext.configure(
            connection=conn,
            opts={
                "target_metadata": metadata,
                "managed_views": managed_views,
                "upgrade_token": "upgrades",
                "downgrade_token": "downgrades",
            },
        )
        return produce_migrations(context, metadata)

    def _view_ops(self, op_list):
        return [
            op
            for op in op_list
            if isinstance(op, (CreateViewOp, DropViewOp))
        ]

    def _apply(self, conn, op_list):
        context = MigrationContext.configure(connection=conn)
        operations = Operations(context)
        for op in self._view_ops(op_list):
            operations.invoke(op)
        conn.commit()


class ViewChainTest(ViewFixture, TestBase):
    def _chain(self, middle_select):
        return [
            ViewDefinition("v_base", "SELECT id, data FROM base"),
            ViewDefinition("v_mid", middle_select, depends_on=["v_base"]),
            ViewDefinition(
                "v_top",
                "SELECT id, upper(data) AS data FROM v_mid",
                depends_on=["v_mid"],
            ),
        ]

    def test_create_chain_in_dependency_order(self):
        conn = self._connect()
        migration_script = self._autogen(
            conn, self._chain("SELECT * FROM v_base")
        )

        eq_(
            [
                (type(op).__name__, op.view_name)
                for op in self._view_ops(migration_script.upgrade_ops.ops)
            ],
            [
                ("CreateViewOp", "v_base"),
                ("CreateViewOp", "v_mid"),
                ("CreateViewOp", "v_top"),
            ],
        )

        self._apply(conn, migration_script.upgrade_ops.ops)
        eq_(
            conn.execute(text("SELECT * FROM v_top ORDER BY id")).fetchall(),
            [(1, "X"), (2, "Y")],
        )

    def test_second_autogenerate_no_changes(self):
        conn = self._connect()
        self._apply(
            conn,
            self._autogen(conn, self._chain("SELECT * FROM v_base"))
            .upgrade_ops.ops,
        )

        migration_script = self._autogen(
            conn, self._chain("SELECT * FROM v_base")
        )
        eq_(self._view_ops(migration_script.upgrade_ops.ops), [])

    def test_middle_view_change_upgrade_and_downgrade(self):
        conn = self._connect()
        self._apply(
            conn,
            self._autogen(conn, self._chain("SELECT * FROM v_base"))
            .upgrade_ops.ops,
        )

        changed = self._chain("SELECT id, data || '!' AS data FROM v_base")
        migration_script = self._autogen(conn, changed)

        # dependents are dropped before the views they depend on, then
        # recreated in the opposite direction
        eq_(
            [
                (type(op).__name__, op.view_name)
                for op in self._view_ops(migration_script.upgrade_ops.ops)
            ],
            [
                ("DropViewOp", "v_top"),
                ("DropViewOp", "v_mid"),
                ("CreateViewOp", "v_mid"),
                ("CreateViewOp", "v_top"),
            ],
        )

        self._apply(conn, migration_script.upgrade_ops.ops)
        eq_(
            conn.execute(text("SELECT * FROM v_top ORDER BY id")).fetchall(),
            [(1, "X!"), (2, "Y!")],
        )

        # downgrade restores the previous definitions read from the
        # database at autogenerate time
        eq_(
            [
                (type(op).__name__, op.view_name)
                for op in self._view_ops(migration_script.downgrade_ops.ops)
            ],
            [
                ("DropViewOp", "v_top"),
                ("DropViewOp", "v_mid"),
                ("CreateViewOp", "v_mid"),
                ("CreateViewOp", "v_top"),
            ],
        )
        self._apply(conn, migration_script.downgrade_ops.ops)
        eq_(
            conn.execute(text("SELECT * FROM v_top ORDER BY id")).fetchall(),
            [(1, "X"), (2, "Y")],
        )

        # after the downgrade, autogenerate against the original
        # declarations is again a no-op
        migration_script = self._autogen(
            conn, self._chain("SELECT * FROM v_base")
        )
        eq_(self._view_ops(migration_script.upgrade_ops.ops), [])


class ViewDefinitionCasesTest(ViewFixture, TestBase):
    def test_selectable_with_create_view_wrapper(self):
        # a declared SELECT that includes a CREATE VIEW wrapper is
        # unwrapped both when compared and when DDL is emitted
        conn = self._connect()
        views = [
            ViewDefinition(
                "v_wrapped", "CREATE VIEW v_wrapped AS SELECT id FROM base"
            )
        ]
        self._apply(conn, self._autogen(conn, views).upgrade_ops.ops)

        eq_(
            conn.execute(text("SELECT * FROM v_wrapped")).fetchall(),
            [(1,), (2,)],
        )
        eq_(self._view_ops(self._autogen(conn, views).upgrade_ops.ops), [])

    def test_view_name_with_spaces(self):
        conn = self._connect()
        views = [ViewDefinition("my view", "SELECT id, data FROM base")]
        migration_script = self._autogen(conn, views)
        self._apply(conn, migration_script.upgrade_ops.ops)

        eq_(
            conn.execute(
                text('SELECT * FROM "my view" ORDER BY id')
            ).fetchall(),
            [(1, "x"), (2, "y")],
        )
        eq_(self._view_ops(self._autogen(conn, views).upgrade_ops.ops), [])

    def test_string_literals_and_quoting_preserved(self):
        select = (
            "SELECT 'a  b' AS val, \"Weird Col\" AS wc, 'it''s' AS q "
            "FROM base"
        )
        conn = self._connect()
        conn.execute(text('ALTER TABLE base ADD COLUMN "Weird Col" VARCHAR'))
        conn.commit()

        views = [ViewDefinition("v_lit", select)]
        self._apply(conn, self._autogen(conn, views).upgrade_ops.ops)

        row = conn.execute(text("SELECT val, q FROM v_lit LIMIT 1")).first()
        eq_(row, ("a  b", "it's"))

        # the comparison must not collapse whitespace inside string
        # literals, strip quoted identifiers, or case-fold the SQL, so a
        # second autogenerate run produces no changes
        eq_(self._view_ops(self._autogen(conn, views).upgrade_ops.ops), [])

    def test_unmanaged_views_are_preserved(self):
        conn = self._connect()
        conn.execute(text("CREATE VIEW v_unmanaged AS SELECT 1 AS one"))
        conn.commit()

        views = [ViewDefinition("v_managed", "SELECT id FROM base")]
        migration_script = self._autogen(conn, views)
        eq_(
            [
                (type(op).__name__, op.view_name)
                for op in self._view_ops(migration_script.upgrade_ops.ops)
            ],
            [("CreateViewOp", "v_managed")],
        )
        self._apply(conn, migration_script.upgrade_ops.ops)

        # the unmanaged view was not dropped and is still not reported
        eq_(
            conn.execute(text("SELECT * FROM v_unmanaged")).fetchall(),
            [(1,)],
        )
        eq_(self._view_ops(self._autogen(conn, []).upgrade_ops.ops), [])

    def test_removed_view_is_dropped_and_restored_on_downgrade(self):
        conn = self._connect()
        views = [ViewDefinition("v_old", "SELECT id, data FROM base")]
        self._apply(conn, self._autogen(conn, views).upgrade_ops.ops)

        # declaring removal: the previously managed view is listed as
        # RemovedView instead of ViewDefinition
        migration_script = self._autogen(conn, [RemovedView("v_old")])
        upgrade = self._view_ops(migration_script.upgrade_ops.ops)
        eq_(
            [(type(op).__name__, op.view_name) for op in upgrade],
            [("DropViewOp", "v_old")],
        )
        # the old SELECT read from the database keeps the op reversible
        eq_(upgrade[0].selectable, "SELECT id, data FROM base")

        self._apply(conn, migration_script.upgrade_ops.ops)
        eq_(inspect(conn).get_view_names(), [])

        self._apply(conn, migration_script.downgrade_ops.ops)
        eq_(
            conn.execute(text("SELECT * FROM v_old ORDER BY id")).fetchall(),
            [(1, "x"), (2, "y")],
        )

    def test_omitted_view_is_not_dropped(self):
        conn = self._connect()
        views = [ViewDefinition("v_keep", "SELECT id FROM base")]
        self._apply(conn, self._autogen(conn, views).upgrade_ops.ops)

        # simply omitting a previously managed view from the collection
        # makes it unmanaged; it is preserved, not dropped
        migration_script = self._autogen(conn, [])
        eq_(self._view_ops(migration_script.upgrade_ops.ops), [])
        eq_(inspect(conn).get_view_names(), ["v_keep"])


class ManagedViewsValidationTest(TestBase):
    def test_cycle_rejected(self):
        assert_raises_message(
            util.CommandError,
            "Dependency cycle detected among managed views: v_a, v_b",
            ManagedViews,
            [
                ViewDefinition("v_a", "SELECT 1", depends_on=["v_b"]),
                ViewDefinition("v_b", "SELECT 1", depends_on=["v_a"]),
            ],
        )

    def test_self_dependency_rejected(self):
        assert_raises_message(
            util.CommandError,
            "Dependency cycle detected among managed views: v_a",
            ManagedViews,
            [ViewDefinition("v_a", "SELECT 1", depends_on=["v_a"])],
        )

    def test_missing_dependency_rejected(self):
        assert_raises_message(
            util.CommandError,
            "View 'v_a' declares a dependency on view 'v_missing', which "
            "is not part of the managed_views collection",
            ManagedViews,
            [ViewDefinition("v_a", "SELECT 1", depends_on=["v_missing"])],
        )

    def test_duplicate_name_rejected(self):
        assert_raises_message(
            util.CommandError,
            "View 'v_a' is declared more than once",
            ManagedViews,
            [
                ViewDefinition("v_a", "SELECT 1"),
                RemovedView("v_a"),
            ],
        )

    def test_dependency_on_removed_view_rejected(self):
        assert_raises_message(
            util.CommandError,
            "View 'v_a' declares a dependency on view 'v_old', which "
            "is not part of the managed_views collection",
            ManagedViews,
            [
                ViewDefinition("v_a", "SELECT 1", depends_on=["v_old"]),
                RemovedView("v_old"),
            ],
        )

    def test_cycle_rejected_before_autogenerate_executes(self):
        eng = create_engine("sqlite://")
        conn = eng.connect()
        try:
            fixture = ViewFixture()
            fixture.connections = []
            assert_raises_message(
                util.CommandError,
                "Dependency cycle detected among managed views",
                fixture._autogen,
                conn,
                [
                    ViewDefinition("v_a", "SELECT 1", depends_on=["v_b"]),
                    ViewDefinition("v_b", "SELECT 1", depends_on=["v_a"]),
                ],
            )
        finally:
            conn.close()


class NormalizeViewSelectTest(TestBase):
    def test_strips_create_view_wrapper(self):
        eq_(
            normalize_view_select(
                'CREATE VIEW IF NOT EXISTS "my view" AS SELECT 1;'
            ),
            "SELECT 1",
        )

    def test_strips_temp_and_or_replace(self):
        eq_(
            normalize_view_select(
                "CREATE OR REPLACE TEMP VIEW v1 AS\nSELECT 1"
            ),
            "SELECT 1",
        )

    def test_plain_select_unchanged(self):
        eq_(normalize_view_select("SELECT 1"), "SELECT 1")

    def test_string_literal_whitespace_not_collapsed(self):
        eq_(
            normalize_view_select("CREATE VIEW v1 AS SELECT 'a  b' AS x"),
            "SELECT 'a  b' AS x",
        )

    def test_quoted_identifiers_and_case_preserved(self):
        eq_(
            normalize_view_select(
                'CREATE VIEW v1 AS SELECT "Weird Col" FROM "Some Table"'
            ),
            'SELECT "Weird Col" FROM "Some Table"',
        )

    def test_body_not_case_folded(self):
        eq_(
            normalize_view_select("create view v1 as SeLeCt 1"),
            "SeLeCt 1",
        )


class ViewRenderTest(TestBase):
    def test_rendered_script_is_self_contained(self):
        from alembic.operations.ops import UpgradeOps

        upgrade_ops = UpgradeOps(
            [
                CreateViewOp(
                    "my view",
                    "SELECT 'a  b' AS val FROM base",
                    depends_on=["v0"],
                ),
                DropViewOp("v_old", selectable="SELECT id FROM base"),
            ]
        )
        rendered = render_python_code(upgrade_ops)
        eq_(
            rendered,
            "# ### commands auto generated by Alembic - please adjust! ###\n"
            "    op.create_view('my view', \"SELECT 'a  b' AS val FROM "
            "base\", depends_on=['v0'])\n"
            "    op.drop_view('v_old', selectable='SELECT id FROM base')\n"
            "    # ### end Alembic commands ###",
        )

    def test_offline_create_and_drop_view(self):
        buf = io.StringIO()
        context = MigrationContext.configure(
            dialect_name="sqlite",
            opts={"as_sql": True, "output_buffer": buf},
        )
        operations = Operations(context)
        operations.create_view("my view", "SELECT 'a  b' AS val FROM base")
        operations.drop_view("my view")

        eq_(
            buf.getvalue(),
            'CREATE VIEW "my view" AS SELECT \'a  b\' AS val FROM base;\n\n'
            'DROP VIEW "my view";\n\n',
        )

    def test_drop_view_without_selectable_not_reversible(self):
        assert_raises_message(
            util.CommandError,
            "Can't reverse a drop_view\(\) operation for view 'v1'",
            DropViewOp("v1").reverse,
        )
