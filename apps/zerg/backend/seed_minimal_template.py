#!/usr/bin/env python3
"""
Clean seed script to create the "minimal" workflow template using proper ORM.
Run this after initializing the database.
"""

import uuid

from zerg.crud import crud
from zerg.database import db_session


def create_minimal_template():
    """Create the minimal workflow template with a manual trigger using proper ORM."""

    # Define the minimal template canvas with a single Manual Trigger node
    minimal_canvas = {
        "nodes": [
            {
                "node_id": str(uuid.uuid4()),
                "node_type": "Trigger",
                "config": {
                    "x": 400.0,
                    "y": 200.0,
                    "width": 200.0,
                    "height": 80.0,
                    "color": "#10b981",
                    "text": "Manual Trigger",
                    "trigger_type": "Manual",
                    "enabled": True,
                    "params": {},
                    "filters": [],
                },
                "position": {"x": 400.0, "y": 200.0},
            }
        ],
        "edges": [],
    }

    with db_session() as db:
        # Check if minimal template already exists
        existing = crud.get_workflow_template_by_name(db=db, template_name="minimal")
        if existing:
            print(f"✓ Minimal template already exists with ID: {existing.id}")
            return existing

        # Create the minimal template using proper CRUD function (clean approach)
        template = crud.create_workflow_template(
            db=db,
            created_by=1,  # System/admin user
            name="minimal",
            description="Minimal workflow template with a manual trigger for getting started",
            category="starter",
            canvas=minimal_canvas,
            tags=["starter", "minimal", "trigger"],  # Proper list now that model is fixed
            preview_image_url=None,
        )

        print(f"✓ Created minimal template with ID: {template.id}")
        return template


if __name__ == "__main__":
    print("🌱 Creating minimal workflow template (clean approach)...")
    template = create_minimal_template()
    print(f"🎉 Minimal template ready: '{template.name}' (ID: {template.id})")
