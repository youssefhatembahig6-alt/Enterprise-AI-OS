"""M1 business sales data: customers, products, orders, order lines, invoices and
the per-tenant import manifest.

Source rows keep their generator UUIDs as ``id``; the primary key is
``(tenant_id, id)`` so the same source dataset could be imported for another
tenant without collisions. The source ``company_id`` is replaced by the M0
``tenant_id`` (mapping recorded in ``business_dataset_imports``). Money columns
are ``numeric(14,2)``. No arithmetic CHECKs are declared: the database must be
able to hold the source baseline exactly, including any source inconsistency,
which is reported by the importer instead.

Revision ID: 0002_business_sales
Revises: 0001_foundation
Create Date: 2026-11-20
"""

from alembic import op

from app.db.roles import app_role_name

revision = "0002_business_sales"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None

BUSINESS_TABLES = (
    "business_dataset_imports",
    "sales_customers",
    "sales_products",
    "sales_orders",
    "sales_order_lines",
    "sales_invoices",
)

SCHEMA = r"""
CREATE TABLE business_dataset_imports (
    tenant_id                   uuid PRIMARY KEY REFERENCES tenants (id) ON DELETE CASCADE,
    dataset                     text NOT NULL,
    generator_version           text NOT NULL,
    source_revision             text NOT NULL CHECK (source_revision ~ '^[0-9a-f]{40}$'),
    seed                        bigint NOT NULL,
    reference_date              date NOT NULL,
    source_company_id           uuid NOT NULL,
    source_content_fingerprint  text NOT NULL CHECK (source_content_fingerprint ~ '^[0-9a-f]{64}$'),
    normalized_fingerprint      text NOT NULL CHECK (normalized_fingerprint ~ '^[0-9a-f]{64}$'),
    row_counts                  jsonb NOT NULL,
    imported_at                 timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sales_customers (
    tenant_id                 uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    id                        uuid NOT NULL,
    name                      text NOT NULL,
    region                    text NOT NULL,
    country                   text NOT NULL CHECK (country ~ '^[A-Z]{2}$'),
    account_owner_source_id   uuid,
    since_date                date NOT NULL,
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE sales_products (
    tenant_id   uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    id          uuid NOT NULL,
    sku         text NOT NULL,
    name        text NOT NULL,
    tier        text NOT NULL,
    unit_price  numeric(14, 2) NOT NULL CHECK (unit_price >= 0),
    currency    text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    is_active   boolean NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, sku)
);

CREATE TABLE sales_orders (
    tenant_id             uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    id                    uuid NOT NULL,
    order_number          text NOT NULL,
    customer_id           uuid NOT NULL,
    sales_rep_source_id   uuid,
    order_date            date NOT NULL,
    region                text NOT NULL,
    status                text NOT NULL CHECK (status IN ('FULFILLED', 'PENDING', 'CANCELLED')),
    subtotal              numeric(14, 2) NOT NULL CHECK (subtotal >= 0),
    tax                   numeric(14, 2) NOT NULL CHECK (tax >= 0),
    total                 numeric(14, 2) NOT NULL CHECK (total >= 0),
    currency              text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, order_number),
    FOREIGN KEY (tenant_id, customer_id) REFERENCES sales_customers (tenant_id, id)
);
CREATE INDEX sales_orders_date_idx ON sales_orders (tenant_id, order_date);

CREATE TABLE sales_order_lines (
    tenant_id   uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    id          uuid NOT NULL,
    order_id    uuid NOT NULL,
    product_id  uuid NOT NULL,
    quantity    integer NOT NULL CHECK (quantity > 0),
    unit_price  numeric(14, 2) NOT NULL CHECK (unit_price >= 0),
    line_total  numeric(14, 2) NOT NULL CHECK (line_total >= 0),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, order_id) REFERENCES sales_orders (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, product_id) REFERENCES sales_products (tenant_id, id)
);
CREATE INDEX sales_order_lines_order_idx ON sales_order_lines (tenant_id, order_id);

CREATE TABLE sales_invoices (
    tenant_id       uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    id              uuid NOT NULL,
    order_id        uuid NOT NULL,
    invoice_number  text NOT NULL,
    issue_date      date NOT NULL,
    due_date        date NOT NULL,
    amount          numeric(14, 2) NOT NULL CHECK (amount >= 0),
    currency        text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    status          text NOT NULL CHECK (status IN ('PAID', 'OPEN', 'OVERDUE')),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, invoice_number),
    FOREIGN KEY (tenant_id, order_id) REFERENCES sales_orders (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX sales_invoices_order_idx ON sales_invoices (tenant_id, order_id);
"""


def _run(sql: str) -> None:
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)


def upgrade() -> None:
    app = app_role_name()
    _run(SCHEMA)
    for table in BUSINESS_TABLES:
        _run(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        _run(
            f"CREATE POLICY tenant_isolation ON {table}"
            " USING (tenant_id = app_current_tenant_id())"
            " WITH CHECK (tenant_id = app_current_tenant_id())"
        )
        # Business data is read-only for the application role; imports run as owner.
        _run(f"GRANT SELECT ON {table} TO {app}")


def downgrade() -> None:
    _run(
        "DROP TABLE IF EXISTS sales_invoices, sales_order_lines, sales_orders, sales_products,"
        " sales_customers, business_dataset_imports CASCADE"
    )
