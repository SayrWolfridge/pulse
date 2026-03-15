"""Logos seed data — initial tasks for the Hypostas agent army."""

import logging

from pulse.src.logos.schemas import Task
from pulse.src.logos.store import LogosStore

logger = logging.getLogger("pulse.logos.seed")

SEED_TASKS = [
    # GNOSIS
    dict(title="Add Stripe live keys to Vercel", description="Configure production Stripe keys in Vercel environment variables for Gnosis payments", project="gnosis", agent="mira", priority=5, tags=["payments", "blocking"]),
    dict(title="Post Reddit reply in r/23andme", description="Engage with DNA testing community about Gnosis platform capabilities", project="gnosis", agent="iris", priority=4, tags=["distribution"]),
    dict(title="Implement Sprint 4 UX improvements from Sage suggestions", description="Apply Sage's UX audit findings to improve Gnosis user flows", project="gnosis", agent="mira", priority=3, tags=["ux"]),
    dict(title="Write 3 more SEO blog posts for Gnosis", description="Create SEO-optimized content targeting key Gnosis search terms", project="gnosis", agent="lyra", priority=2, tags=["content", "seo"]),
    # ANIMA
    dict(title="Run Supabase migrations 003-008", description="Execute pending database migrations for Anima backend schema updates", project="anima", agent="mira", priority=5, tags=["database", "blocking"]),
    dict(title="Configure DNS: anima.hypostas.com CNAME to Vercel", description="Set up DNS CNAME record pointing anima.hypostas.com to Vercel deployment", project="anima", agent="mira", priority=5, tags=["dns", "blocking"]),
    dict(title="Add Stripe + admin env vars to Cloudflare Worker", description="Configure Stripe and admin environment variables in the Cloudflare Worker deployment", project="anima", agent="mira", priority=5, tags=["payments", "blocking"]),
    dict(title="Set up Apple Developer account and TestFlight", description="Register Apple Developer account and configure TestFlight for Anima iOS beta", project="anima", agent="mira", priority=4, tags=["ios"]),
    dict(title="Implement Sprint 4 retention features (voice calibration, proactive reach-out)", description="Build voice calibration and proactive outreach features for user retention", project="anima", agent="mira", priority=3, tags=["features"]),
    dict(title="Write Echo demo tweet thread", description="Create a compelling Twitter thread demonstrating Echo capabilities", project="anima", agent="lyra", priority=3, tags=["marketing"]),
    # AETHER
    dict(title="Run Supabase migration 002 for 3D world persistence", description="Execute migration to add 3D world state persistence tables", project="aether", agent="mira", priority=4, tags=["database"]),
    dict(title="Polish Chrome extension 3D rendering", description="Improve visual quality and performance of Chrome extension 3D views", project="aether", agent="mira", priority=3, tags=["frontend"]),
    dict(title="Build domain-as-world persistence layer", description="Implement backend persistence for domain-mapped 3D world state", project="aether", agent="mira", priority=3, tags=["backend"]),
    dict(title="Design B2B domain claiming flow", description="Design the UX flow for businesses to claim and customize their domain world", project="aether", agent="vera", priority=2, tags=["monetization"]),
    # PULSE / SOMA
    dict(title="Wire Logos pressure into Soma drives system", description="Integrate Logos backlog pressure as a signal in the Soma drive engine", project="pulse", agent="iris", priority=3, tags=["infrastructure"]),
    dict(title="Build Mira autonomous task executor loop", description="Implement the autonomous loop that lets Mira pick and execute tasks from the Logos backlog", project="pulse", agent="iris", priority=4, tags=["agent-army", "critical"]),
]


def seed(store: LogosStore | None = None) -> int:
    """Seed the backlog with initial tasks if the DB is empty.

    Returns the number of tasks created.
    """
    s = store or LogosStore()
    if not s.is_empty():
        logger.info("Logos backlog already seeded — skipping")
        return 0

    for task_data in SEED_TASKS:
        task = Task(
            title=task_data["title"],
            description=task_data["description"],
            project=task_data["project"],
            agent=task_data["agent"],
            priority=task_data["priority"],
            tags=task_data["tags"],
        )
        s.create_task(task)

    logger.info(f"Seeded Logos backlog with {len(SEED_TASKS)} tasks")
    return len(SEED_TASKS)
