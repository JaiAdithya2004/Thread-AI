import json, typer
from brain.engine import Brain

app = typer.Typer(help="Horizontal company brain (Hyper-style).")

def _brain(team: str = None):
    return Brain(team_id=team)

@app.command()
def backfill(
    source: str = typer.Argument("sample", help="The data source to backfill"),
    team: str = typer.Option(None, help="Team to assign ingested content to"),
):
    """Pull full history from a connector, extract facts. Try: brain backfill sample"""
    typer.echo(json.dumps(_brain(team).backfill(source, team_id=team), indent=2))

@app.command()
def sync(
    source: str,
    team: str = typer.Option(None, help="Team to assign new content to"),
):
    typer.echo(json.dumps(_brain(team).sync(source, team_id=team), indent=2))

@app.command()
def search(
    query: str,
    team: str = typer.Option(None, help="Search within a specific team (default: BRAIN_TEAM)"),
    cross_team: bool = typer.Option(False, "--cross-team", help="Search across all teams (privileged)"),
):
    typer.echo(json.dumps(_brain(team).search(query, cross_team=cross_team), indent=2))

@app.command()
def history(
    subject: str,
    team: str = typer.Option(None, help="Team scope"),
    cross_team: bool = typer.Option(False, "--cross-team", help="Search across all teams"),
):
    typer.echo(json.dumps(_brain(team).decision_history(subject, cross_team=cross_team), indent=2))

@app.command()
def record(
    text: str,
    team: str = typer.Option(None, help="Team to record fact for"),
    shared: bool = typer.Option(False, "--shared", help="Mark as company-wide (visible to all teams)"),
):
    """Record a trusted human-authored fact right now."""
    visibility = "shared" if shared else "team"
    typer.echo(json.dumps(_brain(team).record(text, visibility=visibility), indent=2))

@app.command()
def correct(fact_id: str, correction: str):
    typer.echo(json.dumps(Brain().correct(fact_id, correction), indent=2))

@app.command()
def log(
    limit: int = 20,
    team: str = typer.Option(None, help="Filter log by team"),
):
    for r in _brain(team).ledger(limit):
        t = r.get("team_id") or "–"
        typer.echo(f"{r['ts']}  {r['surface']:7}  [{t}]  {r['query']}")

@app.command()
def teams():
    """List all teams in this workspace."""
    rows = Brain().list_teams()
    if not rows:
        typer.echo("No teams yet. Use: brain team-create <id> <name>")
        return
    for t in rows:
        typer.echo(f"{t['id']:20}  {t['name']}")

@app.command("team-create")
def team_create(
    team_id: str = typer.Argument(..., help="Short ID e.g. 'engineering' or 'sales'"),
    name: str = typer.Argument(..., help="Display name"),
    description: str = typer.Option("", help="Optional description"),
):
    """Create or register a team in the workspace."""
    typer.echo(json.dumps(Brain().create_team(team_id, name, description), indent=2))

if __name__ == "__main__":
    app()
