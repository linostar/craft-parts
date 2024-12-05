# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2021-2024 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License version 3 as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""The parts lifecycle manager."""

import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from craft_parts import errors, executor, packages, plugins, sequencer
from craft_parts.actions import Action
from craft_parts.dirs import ProjectDirs
from craft_parts.features import Features
from craft_parts.infos import ProjectInfo
from craft_parts.overlays import LayerHash
from craft_parts.parts import Part, part_by_name
from craft_parts.state_manager import states
from craft_parts.steps import Step
from craft_parts.utils.partition_utils import validate_partition_names


class LifecycleManager:
    """Coordinate the planning and execution of the parts lifecycle.

    The lifecycle manager determines the list of actions that needs be executed in
    order to obtain a tree of installed files from the specification on how to
    process its parts, and provides a mechanism to execute each of these actions.

    :param all_parts: A dictionary containing the parts specification according
        to the :ref:`parts schema<part_properties>`. The format is compatible with the
        output generated by PyYAML's ``yaml.load``.
    :param application_name: A unique non-empty identifier for the application
        using Craft Parts. Valid application names contain upper and lower case
        letters, underscores or numbers, and must start with a letter.
    :param project_name: name of the project being built.
    :param cache_dir: The path to store cached packages and files. If not
        specified, a directory under the application name entry in the XDG base
        directory will be used.
    :param work_dir: The toplevel directory for work directories. The current
        directory will be used if none is specified.
    :param arch: The architecture to build for. Defaults to the host system
        architecture.
    :param base: [deprecated] The system base the project being processed will
        run on. Defaults to the system where Craft Parts is being executed.
    :param parallel_build_count: The maximum number of concurrent jobs to be
        used to build each part of this project.
    :param application_package_name: The name of the application package, if required
        by the package manager used by the platform. Defaults to the application name.
    :param ignore_local_sources: A list of local source patterns to ignore.
    :param extra_build_packages: A list of additional build packages to install.
    :param extra_build_snaps: A list of additional build snaps to install.
    :param track_stage_packages: Add primed stage packages to the prime state.
    :param strict_mode: Only allow plugins capable of building in strict mode.
    :param base_layer_dir: The path to the overlay base layer, if using overlays.
    :param base_layer_hash: The validation hash of the overlay base image, if using
        overlays. The validation hash should be constant for a given image, and should
        change if a different base image is used.
    :param project_vars_part_name: Project variables can only be set in the part
        matching this name.
    :param project_vars: A dictionary containing project variables.
    :param partitions: A list of partitions to use when the partitions feature is
        enabled. The first partition must be "default". Partitions may have an
        optional namespace prefix separated by a forward slash. Partition names
        must contain one or more lowercase alphanumeric characters or hyphens
        ("-"), and may not begin or end with a hyphen.  Namespace names must
        consist of only lowercase alphanumeric characters.
    :param custom_args: Any additional arguments that will be passed directly
        to callbacks.
    """

    def __init__(  # noqa: PLR0913
        self,
        all_parts: dict[str, Any],
        *,
        application_name: str,
        cache_dir: Path | str,
        work_dir: Path | str = ".",
        arch: str = "",
        base: str = "",
        project_name: str | None = None,
        parallel_build_count: int = 1,
        application_package_name: str | None = None,
        ignore_local_sources: list[str] | None = None,
        extra_build_packages: list[str] | None = None,
        extra_build_snaps: list[str] | None = None,
        track_stage_packages: bool = False,
        strict_mode: bool = False,
        base_layer_dir: Path | None = None,
        base_layer_hash: bytes | None = None,
        project_vars_part_name: str | None = None,
        project_vars: dict[str, str] | None = None,
        partitions: list[str] | None = None,
        use_host_sources: bool = False,
        **custom_args: Any,  # custom passthrough args
    ) -> None:
        # pylint: disable=too-many-locals

        if not re.match("^[A-Za-z][0-9A-Za-z_]*$", application_name):
            raise errors.InvalidApplicationName(application_name)

        if not isinstance(all_parts, dict):
            raise TypeError("parts definition must be a dictionary")

        if not application_package_name:
            application_package_name = application_name

        if "parts" not in all_parts:
            raise ValueError("parts definition is missing")

        validate_partition_names(partitions)

        packages.Repository.configure(application_package_name)

        project_dirs = ProjectDirs(work_dir=work_dir, partitions=partitions)

        project_info = ProjectInfo(
            application_name=application_name,
            cache_dir=Path(cache_dir),
            arch=arch,
            base=base,
            parallel_build_count=parallel_build_count,
            strict_mode=strict_mode,
            project_name=project_name,
            project_dirs=project_dirs,
            project_vars_part_name=project_vars_part_name,
            project_vars=project_vars,
            partitions=partitions,
            base_layer_dir=base_layer_dir,
            base_layer_hash=base_layer_hash,
            **custom_args,
        )

        parts_data = all_parts.get("parts", {})

        executor.expand_environment(parts_data, info=project_info)

        part_list = []
        for name, spec in parts_data.items():
            part = _build_part(name, spec, project_dirs, strict_mode, partitions)
            _validate_part_dependencies(part, parts_data)
            part_list.append(part)

        self._has_overlay = any(p.has_overlay for p in part_list)
        self._needs_chisel = any(p.has_slices for p in part_list)
        self._has_chisel = any(p.has_chisel_as_build_snap for p in part_list)

        # add a chisel as a build snap if needed
        if self._needs_chisel and not self._has_chisel:
            if extra_build_snaps is None:
                extra_build_snaps = []
            extra_build_snaps.append("chisel/latest/stable")

        # a base layer is mandatory if overlays are in use
        if self._has_overlay:
            _ensure_overlay_supported()

            if not base_layer_dir:
                raise ValueError("base_layer_dir must be specified if using overlays")
            if not base_layer_hash:
                raise ValueError("base_layer_hash must be specified if using overlays")
        else:
            base_layer_dir = None

        if base_layer_hash:
            layer_hash: LayerHash | None = LayerHash(base_layer_hash)
        else:
            layer_hash = None

        self._part_list = part_list
        self._application_name = application_name
        self._target_arch = project_info.target_arch
        self._sequencer = sequencer.Sequencer(
            part_list=self._part_list,
            project_info=project_info,
            ignore_outdated=ignore_local_sources,
            base_layer_hash=layer_hash,
        )
        self._executor = executor.Executor(
            part_list=self._part_list,
            project_info=project_info,
            ignore_patterns=ignore_local_sources,
            extra_build_packages=extra_build_packages,
            extra_build_snaps=extra_build_snaps,
            track_stage_packages=track_stage_packages,
            base_layer_dir=base_layer_dir,
            base_layer_hash=layer_hash,
            use_host_sources=use_host_sources,
        )
        self._project_info = project_info
        # pylint: enable=too-many-locals

    @property
    def project_info(self) -> ProjectInfo:
        """Obtain information about this project."""
        return self._project_info

    def clean(
        self, step: Step = Step.PULL, *, part_names: list[str] | None = None
    ) -> None:
        """Clean the specified step and parts.

        Cleaning a step removes its state and all artifacts generated in that
        step and subsequent steps for the specified parts.

        :param step: The step to clean. If not specified, all steps will be
            cleaned.
        :param part_names: The list of part names to clean. If not specified,
            all parts will be cleaned and work directories will be removed.
        """
        self._executor.clean(initial_step=step, part_names=part_names)

    def refresh_packages_list(self) -> None:
        """Update the available packages list.

        The list of available packages should be updated before planning the
        sequence of actions to take. To ensure consistency between the scenarios,
        it shouldn't be updated between planning and execution.
        """
        packages.Repository.refresh_packages_list()

    def plan(
        self,
        target_step: Step,
        part_names: Sequence[str] | None = None,
        *,
        rerun: bool = False,
    ) -> list[Action]:
        """Obtain the list of actions to be executed given the target step and parts.

        :param target_step: The final step we want to reach.
        :param part_names: The list of parts to process. If not specified, all
            parts will be processed.

        :return: The list of :class:`Action` objects that should be executed in
            order to reach the target step for the specified parts.
        """
        return self._sequencer.plan(target_step, part_names, rerun=rerun)

    def reload_state(self) -> None:
        """Reload the ephemeral state from disk."""
        self._sequencer.reload_state()

    def action_executor(self) -> executor.ExecutionContext:
        """Return a context manager for action execution."""
        return executor.ExecutionContext(executor=self._executor)

    def get_pull_assets(self, *, part_name: str) -> dict[str, Any] | None:
        """Return the part's pull state assets.

        :param part_name: The name of the part to get assets from.

        :return: The dictionary of the part's pull assets, or None if no state found.
        """
        part = part_by_name(part_name, self._part_list)
        state = cast(states.PullState, states.load_step_state(part, Step.PULL))
        return state.assets if state else None

    def get_primed_stage_packages(self, *, part_name: str) -> list[str] | None:
        """Return the list of primed stage packages.

        :param part_name: The name of the part to get primed stage packages from.

        :return: The sorted list of primed stage packages, or None if no state found.
        """
        part = part_by_name(part_name, self._part_list)
        state = cast(states.PrimeState, states.load_step_state(part, Step.PRIME))
        if not state:
            return None

        return sorted(state.primed_stage_packages)


def _ensure_overlay_supported() -> None:
    """Overlay is only supported in Linux and requires superuser privileges."""
    if not Features().enable_overlay:
        raise errors.FeatureError("Overlays are not supported.")

    if sys.platform != "linux":
        raise errors.OverlayPlatformError

    if os.geteuid() != 0:
        raise errors.OverlayPermissionError


def _build_part(
    name: str,
    spec: dict[str, Any],
    project_dirs: ProjectDirs,
    strict_plugins: bool,  # noqa: FBT001
    partitions: list[str] | None,
) -> Part:
    """Create and populate a :class:`Part` object based on part specification data.

    :param spec: A dictionary containing the part specification.
    :param project_dirs: The project's work directories.

    :return: A :class:`Part` object corresponding to the given part specification.
    """
    if not isinstance(spec, dict):
        raise errors.PartSpecificationError(
            part_name=name, message="part definition is malformed"
        )

    plugin_name = spec.get("plugin", "")

    # If the plugin was not specified, use the part name as the plugin name.
    part_name_as_plugin_name = not plugin_name
    if part_name_as_plugin_name:
        plugin_name = name

    try:
        plugin_class = plugins.get_plugin_class(plugin_name)
    except ValueError as err:
        if part_name_as_plugin_name:
            # If plugin was not specified, avoid raising an exception telling
            # that part name is an invalid plugin.
            raise errors.UndefinedPlugin(part_name=name) from err
        raise errors.InvalidPlugin(plugin_name, part_name=name) from err

    if strict_plugins and not plugin_class.supports_strict_mode:
        raise errors.PluginNotStrict(plugin_name, part_name=name)

    # validate and unmarshal plugin properties
    try:
        properties = plugin_class.properties_class.unmarshal(spec)
    except ValidationError as err:
        raise errors.PartSpecificationError.from_validation_error(
            part_name=name, error_list=err.errors()
        ) from err
    except ValueError as err:
        raise errors.PartSpecificationError(part_name=name, message=str(err)) from err

    part_spec = plugins.extract_part_properties(spec, plugin_name=plugin_name)

    # initialize part and unmarshal part specs
    return Part(
        name,
        part_spec,
        project_dirs=project_dirs,
        plugin_properties=properties,
        partitions=partitions,
    )


def _validate_part_dependencies(part: Part, parts_data: dict[str, Any]) -> None:
    for name in part.dependencies:
        if name not in parts_data:
            raise errors.InvalidPartName(name)
