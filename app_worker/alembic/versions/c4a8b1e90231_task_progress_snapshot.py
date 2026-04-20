"""Task progress snapshot for /queue dashboard.

Revision ID: c4a8b1e90231
Revises: 50331b3c39bb
Create Date: 2026-04-20

"""

import sqlalchemy as sa

from alembic import op

revision = 'c4a8b1e90231'
down_revision = '50331b3c39bb'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('task', sa.Column('progress_snapshot', sa.Text(), nullable=True))
    op.add_column('task', sa.Column('progress_updated_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('task', 'progress_updated_at')
    op.drop_column('task', 'progress_snapshot')
