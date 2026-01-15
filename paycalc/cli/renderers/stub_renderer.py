"""Rich renderer for modeled pay stubs.

Transforms SDK JSON output into formatted Rich tables.
"""

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box


def render_modeled_stub(console: Console, data: dict) -> None:
    """Render modeled stub data as Rich tables.

    Args:
        console: Rich Console instance
        data: SDK output from model_stub()
    """
    # Check for error
    if "error" in data:
        console.print(Panel(
            f"[red]{data['error']}[/red]",
            title="Error",
            border_style="red"
        ))
        return

    # Warnings first
    for warning in data.get("warnings", []):
        console.print(Panel(
            f"[yellow]{warning}[/yellow]",
            title="Note",
            border_style="yellow"
        ))

    # Sources panel
    _render_sources(console, data.get("sources", {}))

    # Main stub table
    _render_stub_table(console, data)


def _render_sources(console: Console, sources: dict) -> None:
    """Render sources panel."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim")
    table.add_column("value")

    source_order = ["comp_plan", "benefits", "w4", "ytd_baseline", "tax_rules"]
    for key in source_order:
        if key in sources:
            label = key.replace("_", " ").title()
            value = _format_source(sources[key])
            table.add_row(label, value)

    console.print(Panel(table, title="Sources", border_style="dim"))


def _format_source(info: dict) -> str:
    """Format source info with color coding."""
    source_type = info.get("type", "unknown")

    if source_type == "registered":
        effective = info.get("effective", info.get("year", "?"))
        return f"registered (effective {effective})"
    elif source_type == "prior_stub":
        stub_date = info.get("stub_date", "?")
        periods = info.get("periods_projected", 0)
        if periods:
            return f"[cyan]from {stub_date} stub[/cyan] + {periods} period(s) projected"
        return f"[cyan]from {stub_date} stub[/cyan]"
    elif source_type == "fallback_stub":
        return f"[yellow]fallback: {info.get('stub_date', '?')} ({info.get('year', '?')})[/yellow]"
    elif source_type == "fallback_plan":
        return f"[yellow]fallback: {info.get('year', '?')} plan[/yellow]"
    elif source_type == "override":
        return "[magenta]override[/magenta]"
    elif source_type == "calculated":
        return "calculated from period"
    elif source_type == "default":
        return f"[dim]default ({info.get('note', '')})[/dim]"
    elif source_type == "not_found":
        return f"[red]not found[/red]"
    else:
        note = info.get("note", "")
        path = info.get("path", "")
        return note or path or str(info)


def _render_stub_table(console: Console, data: dict) -> None:
    """Render main stub table."""
    current = data.get("current", {})
    ytd = data.get("ytd", {})
    pay_date = data.get("pay_date", "?")
    party = data.get("party", "?")
    period = data.get("period_number", "?")

    table = Table(
        title=f"Modeled Pay Stub: {pay_date} ({party}) - Period {period}",
        box=box.ROUNDED,
    )
    table.add_column("", style="bold", min_width=25)
    table.add_column("Current", justify="right", min_width=12)
    table.add_column("YTD", justify="right", min_width=12)

    # Earnings
    table.add_row("[bold]EARNINGS[/bold]", "", "")
    table.add_row("  Gross Pay", _fmt(current.get("gross")), _fmt(ytd.get("gross")))
    table.add_row("", "", "")

    # Pretax deductions
    table.add_row("[bold]PRETAX DEDUCTIONS[/bold]", "", "")
    table.add_row("  401(k)", _fmt(current.get("pretax_401k")), _fmt(ytd.get("pretax_401k")))

    # Individual benefits if available
    benefit_keys = [k for k in current.keys() if k.startswith("pretax_") and k != "pretax_401k" and k != "pretax_benefits"]
    for key in sorted(benefit_keys):
        label = key.replace("pretax_", "").replace("_", " ").title()
        table.add_row(f"  {label}", _fmt(current.get(key)), "")

    if current.get("pretax_benefits", 0) > 0 and not benefit_keys:
        table.add_row("  Benefits (total)", _fmt(current.get("pretax_benefits")), "")

    total_pretax = current.get("pretax_401k", 0) + current.get("pretax_benefits", 0)
    table.add_row("  [dim]Total Pretax[/dim]", f"[dim]{_fmt(total_pretax)}[/dim]", "")
    table.add_row("", "", "")

    # FIT taxable
    table.add_row(
        "FIT Taxable Wages",
        _fmt(current.get("fit_taxable")),
        _fmt(ytd.get("fit_taxable")),
        style="dim",
    )
    table.add_row("", "", "")

    # Taxes
    table.add_row("[bold]TAXES[/bold]", "", "")
    table.add_row("  Federal Income Tax", _fmt(current.get("fit_withheld")), _fmt(ytd.get("fit_withheld")))
    table.add_row("  Social Security", _fmt(current.get("ss_withheld")), _fmt(ytd.get("ss_withheld")))
    table.add_row("  Medicare", _fmt(current.get("medicare_withheld")), _fmt(ytd.get("medicare_withheld")))
    table.add_row("  [dim]Total Taxes[/dim]", f"[dim]{_fmt(current.get('total_taxes'))}[/dim]", "")
    table.add_row("", "", "")

    # Net pay
    table.add_row(
        "[bold green]NET PAY[/bold green]",
        f"[bold green]{_fmt(current.get('net_pay'))}[/bold green]",
        "",
    )

    console.print(table)


def _fmt(amount: float | None) -> str:
    """Format currency amount."""
    if amount is None:
        return "-"
    return f"${amount:,.2f}"
