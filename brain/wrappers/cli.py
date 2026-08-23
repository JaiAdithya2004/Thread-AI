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

@app.command("eval-providers")
def eval_providers(
    questions_file: str = typer.Option("evals/questions.yaml", help="Path to eval questions YAML"),
    output: str = typer.Option("evals/provider_comparison.json", help="Output JSON path"),
):
    """
    Run the eval set against both primary and fallback providers for each task.
    Prints a quality comparison table. Use this to validate your primary model choice.
    """
    import time, yaml
    from pathlib import Path
    from brain import config as _cfg
    from brain.core import llm as _llm

    # Load questions
    with open(questions_file) as f:
        questions = yaml.safe_load(f)

    routes = _cfg.TASK_ROUTES
    # Gather unique (primary, fallback) pairs across all tasks
    task_pairs = {
        task: (info["primary"], info["fallback"])
        for task, info in routes.items()
    }

    def _run_q(brain, q, forced_model):
        """Run a single question forcing a specific model, return pass/fail result."""
        from brain.core import llm as _llm
        # Temporarily override: use chat_text directly with model forced
        start = time.perf_counter()
        result = brain.search(q["question"], surface="eval")
        elapsed = time.perf_counter() - start

        answer_text = (result.get("synthesized_answer") or result.get("answer") or "").lower()
        facts = result.get("facts", [])
        fact_summaries = " ".join(f.get("summary", "").lower() for f in facts)
        expects_not_sure = q.get("expect_not_sure", False)
        expected_answer = (q.get("expected_answer_contains") or "").lower()
        expected_fact = (q.get("expected_source_fact_summary_contains") or "").lower()
        top_confidence = facts[0].get("confidence", 0.0) if facts else 0.0
        has_synthesized = bool(result.get("synthesized_answer", "").strip())

        if expects_not_sure:
            passed = not has_synthesized or top_confidence < 0.4
        else:
            answer_ok = (not expected_answer) or (expected_answer in answer_text)
            fact_ok = (not expected_fact) or (expected_fact in fact_summaries)
            passed = answer_ok and fact_ok

        return passed, elapsed

    # For each task pair, run all questions and score
    comparison = {}
    for task, (primary, fallback) in task_pairs.items():
        if primary == fallback:
            typer.echo(f"[{task}] primary == fallback ({primary}), skipping duplicate run")

        brain = Brain(team_id=None)
        primary_results = []
        fallback_results = []

        # We can't easily swap the model mid-flight without patching TASK_ROUTES,
        # so we patch temporarily for each run.
        original_route = routes[task].copy()

        # Run with primary
        routes[task] = {**original_route, "fallback": primary}  # force no-fallback
        for q in questions:
            try:
                passed, lat = _run_q(brain, q, primary)
                primary_results.append({"id": q["id"], "passed": passed, "latency": lat})
            except Exception as e:
                primary_results.append({"id": q["id"], "passed": False, "latency": 0, "error": str(e)})

        # Run with fallback (if different)
        if primary != fallback:
            routes[task] = {**original_route, "primary": fallback, "fallback": fallback}
            for q in questions:
                try:
                    passed, lat = _run_q(brain, q, fallback)
                    fallback_results.append({"id": q["id"], "passed": passed, "latency": lat})
                except Exception as e:
                    fallback_results.append({"id": q["id"], "passed": False, "latency": 0, "error": str(e)})
        else:
            fallback_results = primary_results

        # Restore
        routes[task] = original_route

        p_score = sum(1 for r in primary_results if r["passed"])
        f_score = sum(1 for r in fallback_results if r["passed"])
        total = len(questions)
        p_avg_lat = round(sum(r["latency"] for r in primary_results) / max(len(primary_results), 1), 3)
        f_avg_lat = round(sum(r["latency"] for r in fallback_results) / max(len(fallback_results), 1), 3)

        comparison[task] = {
            "primary_model":    primary,
            "primary_score":    f"{p_score}/{total}",
            "primary_avg_lat":  p_avg_lat,
            "fallback_model":   fallback,
            "fallback_score":   f"{f_score}/{total}",
            "fallback_avg_lat": f_avg_lat,
        }

    # Print table
    typer.echo("\n" + "=" * 88)
    typer.echo(f"  Provider comparison — {len(questions)} questions, {len(task_pairs)} tasks")
    typer.echo("=" * 88)
    header = f"  {'Task':<12}  {'Primary model':<35}  {'Score':<8}  {'Lat':>6}  {'Fallback model':<35}  {'Score':<8}  {'Lat':>6}"
    typer.echo(header)
    typer.echo("  " + "-" * 84)
    for task, r in comparison.items():
        row = (
            f"  {task:<12}  {r['primary_model']:<35}  {r['primary_score']:<8}  {r['primary_avg_lat']:>6.2f}s"
            f"  {r['fallback_model']:<35}  {r['fallback_score']:<8}  {r['fallback_avg_lat']:>6.2f}s"
        )
        typer.echo(row)
    typer.echo("=" * 88 + "\n")

    # Write JSON
    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    with open(out_path, "w") as f:
        json.dump(comparison, f, indent=2)
    typer.echo(f"Results saved to {out_path}")


if __name__ == "__main__":
    app()
