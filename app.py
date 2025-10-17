from __future__ import annotations

import asyncio
import base64
import tempfile
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING, Annotated

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from github import Github
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from github.AuthenticatedUser import AuthenticatedUser


# ------------------------------------------------------------
# Settings
# ------------------------------------------------------------
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    secret: str
    github_token: str
    openai_api_key: str
    openai_base_url: str


settings = Settings()  # type: ignore


# ------------------------------------------------------------
# Models
# ------------------------------------------------------------
class Attachment(BaseModel):
    name: str
    url: str


class Task(BaseModel):
    """Task description for the project to create."""

    email: str
    secret: str
    task: str
    round: int
    nonce: str
    brief: str
    checks: list[str]
    evaluation_url: str
    attachments: list[Attachment]


class TaskResponse(BaseModel):
    status: str
    message: str
    task_id: str


class AgentDependencies(BaseModel):
    task: Task
    attachments_dir: Path


class RepoOutput(BaseModel):
    html_content: Annotated[
        str, Field(description="The html should be github pages compatible")
    ]
    readme_content: Annotated[
        str,
        Field(
            description="README should explain how to setup and install this application"
        ),
    ]
    additional_files: Annotated[
        dict[str, str],
        Field(
            default_factory=dict,
            description="Additional files needed for the application.",
        ),
    ]


# ------------------------------------------------------------
# Agent configuration
# ------------------------------------------------------------
code_agent = Agent(
    "openai:gpt-4o",
    deps_type=AgentDependencies,
    output_type=RepoOutput,
    instructions=(
        "You are an expert web developer. Create production-ready, single-page web applications "
        "based on the given brief. Generate complete, functional HTML with embedded CSS and JS. "
        "Follow best practices, responsive design, and clear commenting. Include CDN links."
    ),
)


@code_agent.tool
async def get_attachment_content(
    ctx: RunContext[AgentDependencies], filename: str
) -> str:
    """Retrieve attachment content by name."""
    path = ctx.deps.attachments_dir / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"Attachment '{filename}' not found in {ctx.deps.attachments_dir}"
        )

    try:
        return path.read_text("utf-8")
    except UnicodeDecodeError:
        data = path.read_bytes()
        preview = base64.b64encode(data[:128]).decode()
        return f"Binary file preview (base64, first 128B): {preview}"


@code_agent.instructions
async def add_task_context(ctx: RunContext[AgentDependencies]) -> str:
    t = ctx.deps.task
    checks = "\n".join(f"- {c}" for c in t.checks)
    attachments = ", ".join(a.name for a in t.attachments) or "None"
    return dedent(f"""
        Task Brief: {t.brief}

        Round: {t.round}
        Checks:
        {checks}

        Attachments: {attachments}

        Requirements:
        1. Generate a single responsive HTML file (index.html)
        2. Include Bootstrap 5 if needed
        3. Pass all checks
        4. Add comments and good UX
    """)


# ------------------------------------------------------------
# Utility functions
# ------------------------------------------------------------
async def download_attachments(task: Task, work_dir: Path) -> Path:
    """Download all attachments into a directory."""
    dir_ = work_dir / "attachments"
    dir_.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient() as client:
        for a in task.attachments:
            if a.url.startswith("data:"):
                _, data = a.url.split(",", 1)
                content = base64.b64decode(data)
            else:
                resp = await client.get(a.url)
                resp.raise_for_status()
                content = resp.content
            (dir_ / a.name).write_bytes(content)
    return dir_


def create_repo_name(task: Task) -> str:
    return f"project-{task.task.replace(':', '-')}"


# ------------------------------------------------------------
# Evaluation callback
# ------------------------------------------------------------
async def notify_evaluation(task: Task, repo_url: str, sha: str, pages_url: str):
    payload = {
        "email": task.email,
        "task": task.task,
        "round": task.round,
        "nonce": task.nonce,
        "repo_url": repo_url,
        "commit_sha": sha,
        "pages_url": pages_url,
    }

    async with httpx.AsyncClient() as client:
        for i, delay in enumerate([1, 2, 4, 8]):
            try:
                print(f"Attempt {i + 1}: Notifying {task.evaluation_url}")
                resp = await client.post(
                    task.evaluation_url, json=payload, timeout=30.0
                )
                resp.raise_for_status()
                print("✅ Evaluation server acknowledged.")
                return
            except Exception as e:
                print(f"❌ Attempt {i + 1} failed: {e}")
                if i == 3:
                    raise
                await asyncio.sleep(delay)


# ------------------------------------------------------------
# Main processing logic
# ------------------------------------------------------------
async def process_task(task: Task):
    repo_name = create_repo_name(task)
    github = Github(settings.github_token)
    user = github.get_user()

    with tempfile.TemporaryDirectory() as tmpdirname:
        tmpdir = Path(tmpdirname)

        attachments_dir = await download_attachments(task, tmpdir)
        deps = AgentDependencies(task=task, attachments_dir=attachments_dir)

        result = await code_agent.run(
            f"Create or update the web app for round {task.round}.", deps=deps
        )

        repo_url, commit, pages_url = await manage_and_deploy_repo(
            repo_name,
            user,
            result.output,
        )

        await notify_evaluation(task, repo_url, commit, pages_url)


async def manage_and_deploy_repo(
    repo_name: str,
    user: AuthenticatedUser,
    output: RepoOutput,
) -> tuple[str, str, str]:
    repo = user.create_repo(repo_name, private=False, license_template="MIT")

    repo.create_file(
        "index.html", "Created index.html", content=output.html_content, branch="main"
    )
    creation_data = repo.create_file(
        "README.md", "Created README.md", content=output.readme_content, branch="main"
    )

    for name, content in output.additional_files.items():
        creation_data = repo.create_file(
            name, f"Created {name}", content=content, branch="main"
        )

    commit = creation_data.get("commit")
    if commit is None:
        raise ValueError("Unable to commit to repo")

    # Creating Github manually as its not currently supported by ``Github``
    api_url = repo.url
    async with httpx.AsyncClient(base_url=api_url) as client:
        response = await client.post(
            "/pages",
            headers={
                "Authorization": f"token {settings.github_token}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={"source": {"branch": "main", "path": "/"}},
        )

        response.raise_for_status()

    pages_url = f"https://{user.login.lower()}.github.io/{repo_name.lower()}"

    return repo.clone_url, commit.sha, pages_url


# ------------------------------------------------------------
# FastAPI endpoint
# ------------------------------------------------------------
app = FastAPI()

origins = [
    "*",  # allow all origins, or replace with specific domains
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],  # GET, POST, PUT, DELETE etc.
    allow_headers=["*"],  # allow custom headers
)


@app.post("/task")
def create_task(task: Task, background: BackgroundTasks) -> TaskResponse:
    if task.secret != settings.secret:
        raise HTTPException(status_code=404, detail="Invalid secret key")

    background.add_task(process_task, task)

    return TaskResponse(
        status="accepted",
        message="Task accepted and being processed.",
        task_id=f"{task.task}-{task.round}",
    )
