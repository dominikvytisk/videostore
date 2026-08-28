"""Thin wrapper around rich for the CLI's progress output (see docs/development.md
for the UX this is trying to match)."""
from __future__ import annotations

from contextlib import contextmanager

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

console = Console()


def step(msg: str) -> None:
    console.print(f"[bold cyan]{msg}[/bold cyan]")


def info(msg: str) -> None:
    console.print(msg)


def warn(msg: str) -> None:
    console.print(f"[yellow]warning:[/yellow] {msg}")


def error(msg: str) -> None:
    console.print(f"[bold red]error:[/bold red] {msg}")


def ok(msg: str) -> None:
    console.print(f"[bold green]{msg}[/bold green]")


@contextmanager
def bar(description: str, total: int):
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task(description, total=total)

        def update(n: int = 1) -> None:
            progress.update(task, advance=n)

        yield update
