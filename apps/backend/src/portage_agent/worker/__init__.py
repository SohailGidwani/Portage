"""Queue consumer that runs the LangGraph agent."""

from .queue import PostgresJobQueue

__all__ = ["PostgresJobQueue"]
