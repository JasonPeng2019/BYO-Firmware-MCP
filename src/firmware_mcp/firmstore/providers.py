"""Flat persisted direct-argv provider recipes.

This is deliberately data rather than a provider registry: a recipe supplies one
inventory command and one implementation of the existing isolated worker protocol.
The commands are trusted operator input, but their results are validated because
they become physical connection and target evidence.
"""

from __future__ import annotations

import json
from hashlib import sha256
import subprocess
from dataclasses import dataclass

from firmware_mcp.firmstore.provider_lock import provider_recipe_publication_lock
from firmware_mcp.firmstore.store import FirmStore


class ProviderRecipeError(RuntimeError):
    """A provider recipe or its inventory evidence was not usable."""


def _argv(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ProviderRecipeError(f"{name} must be a non-empty argv array")
    argv = tuple(value)
    if any(not isinstance(part, str) or not part or "\0" in part for part in argv):
        raise ProviderRecipeError(f"{name} entries must be non-empty NUL-free strings")
    return argv


def _provider_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ProviderRecipeError("provider_id must be a non-empty NUL-free string")
    identifier = value.strip()
    if "/" in identifier or "\\" in identifier:
        raise ProviderRecipeError("provider_id must not contain path separators")
    if ":" in identifier:
        raise ProviderRecipeError(
            "provider_id must not contain ':'; provider:<provider_id>:<connection_id> reserves ':'"
        )
    return identifier


def _connection_id(value: object) -> str:
    """Validate one provider-local ID before it enters the namespaced route."""

    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ProviderRecipeError("connection_id must be a non-empty NUL-free string")
    identifier = value.strip()
    if "/" in identifier or "\\" in identifier:
        raise ProviderRecipeError("connection_id must not contain path separators")
    if ":" in identifier:
        raise ProviderRecipeError(
            "connection_id must not contain ':'; provider:<provider_id>:<connection_id> reserves ':'"
        )
    return identifier


@dataclass(frozen=True, slots=True)
class ProviderRecipe:
    provider_id: str
    inventory_argv: tuple[str, ...]
    worker_argv: tuple[str, ...]

    @classmethod
    def from_record(cls, value: object) -> "ProviderRecipe":
        if not isinstance(value, dict) or set(value) != {
            "provider_id",
            "inventory_argv",
            "worker_argv",
        }:
            raise ProviderRecipeError(
                "provider recipe must contain exactly provider_id, inventory_argv, and worker_argv"
            )
        return cls(
            _provider_id(value["provider_id"]),
            _argv(value["inventory_argv"], "inventory_argv"),
            _argv(value["worker_argv"], "worker_argv"),
        )

    def to_record(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "inventory_argv": list(self.inventory_argv),
            "worker_argv": list(self.worker_argv),
        }

    def support_identity(self, target: str) -> str:
        """Return the canonical support binding for this recipe and target.

        This is evidence, not a routing identifier.  Canonical JSON makes a
        changed argv or selected target visible to profile replay and to the
        worker before a generic session is opened.
        """

        if not isinstance(target, str) or not target.strip() or "\0" in target:
            raise ProviderRecipeError("target must be a non-empty NUL-free string")
        selected_target = target.strip()
        document = {
            "recipe": self.to_record(),
            "target": selected_target,
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return f"provider-recipe:{selected_target}:{sha256(encoded.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ProviderRecipeSnapshot:
    """The complete pre-publication recipe-store state."""

    existed: bool
    recipes: tuple[ProviderRecipe, ...]


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    """One observed provider-local connection, namespaced for server routing."""

    provider_id: str
    connection_id: str
    description: str
    probe_uid: str | None
    probe_family: str

    @property
    def namespaced_id(self) -> str:
        return f"provider:{self.provider_id}:{self.connection_id}"

    def to_record(self) -> dict[str, object]:
        return {
            "connection_id": self.namespaced_id,
            "provider_id": self.provider_id,
            "provider_connection_id": self.connection_id,
            "description": self.description,
            "probe_uid": self.probe_uid,
            "probe_family": self.probe_family,
        }


def run_inventory(
    recipe: ProviderRecipe, *, timeout_seconds: float | None = None
) -> tuple[ProviderConnection, ...]:
    """Run one direct inventory argv and validate its exact evidence document."""

    try:
        completed = subprocess.run(
            recipe.inventory_argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ProviderRecipeError(
            f"provider inventory could not start: {type(exc).__name__}: {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderRecipeError(f"provider inventory timed out: {exc}") from exc
    if completed.returncode != 0:
        raise ProviderRecipeError(
            f"provider inventory exited {completed.returncode}; stderr: {completed.stderr.strip()}"
        )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProviderRecipeError(f"provider inventory returned invalid JSON: {exc}") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"connections"}
        or not isinstance(document["connections"], list)
    ):
        raise ProviderRecipeError("provider inventory must return exactly {'connections': [...]}")
    connections: list[ProviderConnection] = []
    identifiers: set[str] = set()
    for item in document["connections"]:
        fields = {"connection_id", "description", "probe_uid", "probe_family"}
        if not isinstance(item, dict) or set(item) != fields:
            raise ProviderRecipeError("provider inventory connection schema was invalid")
        connection_id, description, uid, family = (
            item["connection_id"],
            item["description"],
            item["probe_uid"],
            item["probe_family"],
        )
        if (
            not isinstance(description, str)
            or not description.strip()
            or uid is not None
            and (not isinstance(uid, str) or not uid.strip())
            or not isinstance(family, str)
            or not family.strip()
        ):
            raise ProviderRecipeError("provider inventory connection fields were invalid")
        connection_id = _connection_id(connection_id)
        if connection_id in identifiers:
            raise ProviderRecipeError(
                f"provider inventory duplicated connection_id {connection_id!r}"
            )
        identifiers.add(connection_id)
        connections.append(
            ProviderConnection(
                recipe.provider_id, connection_id, description.strip(), uid, family.strip()
            )
        )
    return tuple(connections)


class ProviderRecipeStore:
    """The one owner of schema-v1 `.firm/providers.json`."""

    VERSION = 1

    def __init__(self, store: FirmStore) -> None:
        self.store = store

    def load_all(self) -> dict[str, ProviderRecipe]:
        path = self.store.layout.providers
        if not path.exists():
            return {}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderRecipeError(f"stored provider recipes are malformed: {exc}") from exc
        if (
            not isinstance(document, dict)
            or set(document) != {"schema_version", "recipes"}
            or document["schema_version"] != self.VERSION
            or not isinstance(document["recipes"], list)
        ):
            raise ProviderRecipeError("stored provider recipes have an invalid schema")
        recipes = [ProviderRecipe.from_record(item) for item in document["recipes"]]
        result = {recipe.provider_id: recipe for recipe in recipes}
        if len(result) != len(recipes):
            raise ProviderRecipeError("stored provider recipes duplicate provider_id")
        return result

    def load(self, provider_id: str) -> ProviderRecipe:
        try:
            return self.load_all()[provider_id]
        except KeyError as exc:
            raise ProviderRecipeError(
                f"provider recipe {provider_id!r} is missing; call get_setup_overview or setup_board with provider_recipe"
            ) from exc

    def save(self, recipe: ProviderRecipe) -> ProviderRecipe:
        with provider_recipe_publication_lock(self.store.layout.project_root):
            recipes = self.load_all()
            recipes[recipe.provider_id] = recipe
            self.replace_all(recipes)
            return recipe

    def snapshot(self) -> "ProviderRecipeSnapshot":
        """Capture recipe contents and whether the store file existed.

        Setup publication needs to restore absence as well as recipe content: an
        empty replacement document is not the same persisted state as no recipe
        store at all.
        """

        with provider_recipe_publication_lock(self.store.layout.project_root):
            return ProviderRecipeSnapshot(
                existed=self.store.layout.providers.exists(),
                recipes=tuple(self.load_all().values()),
            )

    def restore_snapshot(self, snapshot: "ProviderRecipeSnapshot") -> None:
        """Restore a snapshot created before a paired profile publication."""

        with provider_recipe_publication_lock(self.store.layout.project_root):
            if not snapshot.existed:
                path = self.store.layout.providers
                if path.exists():
                    path.unlink()
                return
            self.replace_all({recipe.provider_id: recipe for recipe in snapshot.recipes})

    def replace_all(self, recipes: dict[str, ProviderRecipe]) -> None:
        """Atomically replace the complete recipe document for setup rollback."""

        with provider_recipe_publication_lock(self.store.layout.project_root):
            if any(provider_id != recipe.provider_id for provider_id, recipe in recipes.items()):
                raise ProviderRecipeError("provider recipe rollback mapping is inconsistent")
            self.store.atomic_write_json(
                self.store.layout.providers,
                {
                    "schema_version": self.VERSION,
                    "recipes": [
                        item.to_record()
                        for item in sorted(recipes.values(), key=lambda item: item.provider_id)
                    ],
                },
            )
