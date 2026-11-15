#!/bin/sh
set -eu

alembic upgrade head
python -m app.operations.checkpoint_setup
