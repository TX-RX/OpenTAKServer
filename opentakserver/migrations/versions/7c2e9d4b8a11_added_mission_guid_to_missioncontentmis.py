"""Added mission_guid to MissionContentMission

Revision ID: 7c2e9d4b8a11
Revises: 00442761c803
Create Date: 2026-05-13 19:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7c2e9d4b8a11'
down_revision = '00442761c803'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('mission_content_mission', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mission_guid', sa.String(length=255), nullable=True))
        batch_op.create_index(
            'ix_mission_content_mission_mission_guid', ['mission_guid'], unique=False
        )

    op.execute(
        """
        UPDATE mission_content_mission
        SET mission_guid = (
            SELECT missions.guid
            FROM missions
            WHERE missions.name = mission_content_mission.mission_name
        )
        WHERE mission_guid IS NULL
        """
    )


def downgrade():
    with op.batch_alter_table('mission_content_mission', schema=None) as batch_op:
        batch_op.drop_index('ix_mission_content_mission_mission_guid')
        batch_op.drop_column('mission_guid')
