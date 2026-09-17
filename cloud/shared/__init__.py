"""What the API and the worker agree on: the job model, its schemas, its storage.

Both sides of the future queue import from here and from nowhere else in each
other, so the API can be deployed without the worker's dependencies and the
worker without FastAPI.
"""
