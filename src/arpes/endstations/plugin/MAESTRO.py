"""Implements data loading for the Beamline 7 (MAESTRO) ARPES experiments at ALS.

Common code is provided by a base class reflecting DAQ similarities between micro- and nanoARPES
at MAESTRO. This is subclassed for the individual experiments to handle some subtle differences
in how nanoARPES handles its spatial coordiantes (they are hierarchical) and in the spectrometers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import numpy as np

from arpes.endstations import (
    FITSEndstation,
    HemisphericalEndstation,
    SynchrotronEndstation,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import xarray as xr
    from _typeshed import Incomplete

    from arpes.constants import Spectrometer
    from arpes.endstations import ScanDesc

__all__ = ("MAESTROMicroARPESEndstation", "MAESTRONanoARPESEndstation")


class MAESTROARPESEndstationBase(SynchrotronEndstation, HemisphericalEndstation, FITSEndstation):
    """Common code for the MAESTRO ARPES endstations at the Advanced Light Source."""

    PRINCIPAL_NAME = ""
    ALIASES = []
    ANALYZER_INFORMATION = None

    def load(self, scan_desc: ScanDesc | None = None, **kwargs: Incomplete) -> xr.Dataset:
        # in the future, can use a regex in order to handle the case where we postfix coordinates
        # for multiple spectra
        """[TODO:summary].

        Args:
            scan_desc: [TODO:description]
            kwargs: [TODO:description]
        """
        scan = super().load(scan_desc, **kwargs)

        coord_names = scan.coords.keys()
        will_rename = {}
        for coord_name in coord_names:
            if coord_name in self.RENAME_KEYS:
                will_rename[coord_name] = self.RENAME_KEYS.get(coord_name)

        for v in will_rename.values():
            if v in scan.coords:
                del scan.coords[v]

        renamed = scan.rename(will_rename)

        if "scan_x" in renamed.coords:
            for d in renamed.data_vars:
                if isinstance(d, str) and "spectrum" in d:
                    renamed[d].values = np.flip(
                        renamed[d].values,
                        axis=renamed[d].dims.index("scan_x"),
                    )

        return renamed

    def fix_prebinned_coordinates(self) -> None:
        pass

    def postprocess_final(self, data: xr.Dataset, scan_desc: ScanDesc | None = None) -> xr.Dataset:
        ls = [data, *data.S.spectra]
        for _ in ls:
            _.attrs.update(self.ANALYZER_INFORMATION)

            if "GRATING" in _.attrs:
                _.attrs["grating_lines_per_mm"] = {
                    "G201b": 600,
                }.get(_.attrs["GRATING"])

        return super().postprocess_final(data, scan_desc)


class MAESTROMicroARPESEndstation(MAESTROARPESEndstationBase):
    """Implements data loading at the microARPES endstation of ALS's MAESTRO."""

    PRINCIPAL_NAME = "ALS-BL7"
    ALIASES: ClassVar[list[str]] = ["BL7", "BL7.0.2", "ALS-BL7.0.2", "MAESTRO"]

    ANALYZER_INFORMATION: ClassVar[Spectrometer] = {
        "analyzer": "R4000",
        "analyzer_name": "Scienta R4000",
        "parallel_deflectors": False,
        "perpendicular_deflectors": True,
        "analyzer_radius": np.nan,
        "analyzer_type": "hemispherical",
    }

    RENAME_KEYS: ClassVar[dict] = {
        "LMOTOR0": "x",
        "LMOTOR1": "y",
        "LMOTOR2": "z",
        "Scan X": "scan_x",
        "Scan Y": "scan_y",
        "Scan Z": "scan_z",
        "LMOTOR3": "theta",
        "LMOTOR4": "beta",
        "LMOTOR5": "chi",
        "LMOTOR6": "alpha",
        "LMOTOR9": "psi",
        "mono_eV": "hv",
        "SF_HV": "hv",
        "SS_HV": "hv",
        "Slit Defl": "psi",
        "S_Volts": "volts",
        # probably need something like an attribute list for extraction
        "SFRGN0": "fixed_region_name",
        "SFE_0": "daq_center_energy",
        "SFLNM0": "lens_mode_name",
        "SFPE_0": "pass_energy",
        "UNDHARM": "undulator_harmonic",
        "RINGCURR": "beam_current",
        "SFFR_0": "frames_per_slice",
        "SFBA_0": "phi_prebinning",
        "SFBE0": "eV_prebinning",
        "LWLVNM": "daq_type",
    }

    RENAME_COORDS: ClassVar[dict] = {
        "X": "x",
        "Y": "y",
        "Z": "z",
    }

    ATTR_TRANSFORMS: ClassVar[dict[str, Callable[..., dict[str, int | list[str] | str]]]] = {
        "START_T": lambda _: {
            "time": " ".join(_.split(" ")[1:]).lower(),
            "date": _.split(" ")[0],
        },
        "SF_SLITN": lambda _: {
            "slit_number": int(_.split(" ")[0]),
            "slit_shape": _.split(" ")[-1].lower(),
            "slit_width": float(_.split(" ")[2]),
        },
    }

    MERGE_ATTRS: ClassVar[Spectrometer] = {
        "mcp_voltage": np.nan,
        "repetition_rate": 5e8,
        "undulator_type": "elliptically_polarized_undulator",
        "undulator_gap": None,
        "undulator_z": None,
        "undulator_polarization": None,
    }


class MAESTRONanoARPESEndstation(MAESTROARPESEndstationBase):
    """Implements data loading at the nanoARPES endstation of ALS's MAESTRO."""

    PRINCIPAL_NAME = "ALS-BL7-nano"
    ALIASES: ClassVar[list[str]] = ["BL7-nano", "BL7.0.2-nano", "ALS-BL7.0.2-nano", "MAESTRO-nano"]

    ENSURE_COORDS_EXIST: ClassVar[set[str]] = {
        "long_x",
        "long_y",
        "long_z",
        "short_x",
        "short_y",
        "short_z",
        "theta",
        "beta",
        "chi",
        "hv",
        "alpha",
        "psi",
        "physical_long_x",
        "physical_long_y",
        "physical_long_z",
    }

    ANALYZER_INFORMATION: ClassVar[dict[str, str | float]] = {
        "analyzer": "DA-30",
        "analyzer_name": "Scienta DA-30",
        "parallel_deflectors": False,
        "perpendicular_deflectors": False,
        "analyzer_radius": np.nan,
        "analyzer_type": "hemispherical",
    }

    RENAME_KEYS: ClassVar[dict[str, str]] = {
        "LMOTOR0": "long_x",
        "LMOTOR1": "long_z",
        "LMOTOR2": "long_y",
        "PMOTOR0": "physical_long_x",
        "PMOTOR1": "physical_long_z",
        "PMOTOR2": "physical_long_y",
        "LMOTOR3": "chi",
        "LMOTOR4": "theta",
        "LMOTOR5": "order_sorting_aperature_x",
        "LMOTOR6": "order_sorting_aperature_z",
        "LMOTOR7": "order_sorting_aperature_y",
        "LMOTOR8": "optics_insertion",
        "LMOTOR9": "optics_select",
        "LMOTOR10": "short_x",  # these are the scan stages
        "LMOTOR11": "short_z",
        "LMOTOR12": "short_y",
        "LMOTOR13": "psi",  # Chamber Theta
        "LMOTOR14": "alpha",
        "LMOTOR15": "optics_pitch",
        "LMOTOR16": "optics_yaw",
        "LMOTOR17": "aperature_x",
        "LMOTOR18": "aperature_y",
        "LMOTOR19": "sample_float",
        "MONO_E": "hv",
        "E_RESOLU": "probe_linewidth",
        "EPU_E": "undulator_hv",
        "EPU_POL": "probe_polarization",
        "EPU_Z": "undulator_z",
        "EPU_GAP": "undulator_gap",
        "UNDHARM": "undulator_harmonic",
        "RINGCURR": "beam_current",
        "SF_HV": "hv",
        "SS_HV": "hv",
        "Slit Defl.": "psi",
        # probably need something like an attribute list for extraction
        "SFRGN0": "fixed_region_name",
        "SFE_0": "daq_center_energy",
        "SFLNM0": "lens_mode_name",
        "SFPE_0": "pass_energy",
        "SFFR_0": "frames_per_slice",
        "SFBA_0": "phi_prebinning",
        "SFBE0": "eV_prebinning",
        "LWLVNM": "daq_type",
    }

    RENAME_COORDS: ClassVar[dict[str, str]] = {
        "X": "long_x",
        "Y": "long_y",
        "Z": "long_z",
        "Scan X": "short_x",
        "Scan Y": "short_y",
        "Scan Z": "short_z",
        "Sample X": "long_x",
        "Sample Y": "long_y",
        "Sample Z": "long_z",
        "Optics Stage": "optics_insertion",
        "Pitch": "pitch",
        "S_Volts": "volts",
        "Slit Defl.": "psi",
    }

    ATTR_TRANSFORMS: ClassVar[dict[str, Callable[..., dict[str, float | list[str] | str]]]] = {
        "START_T": lambda _: {
            "time": " ".join(_.split(" ")[1:]).lower(),
            "date": _.split(" ")[0],
        },
        "SF_SLITN": lambda _: {
            "slit_number": int(_.split(" ")[0]),
            "slit_shape": _.split(" ")[-1].lower(),
            "slit_width": float(_.split(" ")[2]),
        },
    }

    MERGE_ATTRS: ClassVar[Spectrometer] = {
        "mcp_voltage": np.nan,
        "beta": 0,
        "repetition_rate": 5e8,
        "undulator_type": "elliptically_polarized_undulator",
        "undulator_gap": None,
        "undulator_z": None,
        "undulator_polarization": None,
    }

    @staticmethod
    def update_hierarchical_coordinates(data: xr.Dataset) -> xr.Dataset:
        """Converts long and short coordinates to a single standard coordinate.

        Nano-ARPES endstations often have two sets of spatial coordinates, a long-range piezo
        inertia or stepper stage, sometimes outside vacuum, and a fast, high resolution piezo scan
        stage that may or may not be based on piezo inertia ("slip-stick") type actuators.

        Additionally, any spatially imaging experiments like PEEM or the transmission operating mode
        of hemispherical analyzers have two spatial coordinates, the one on the manipulator and the
        imaged axis. In these cases, this imaged axis will always be treated in the same role as the
        high-resolution motion axis of a nano-ARPES system.

        Working in two coordinate systems is frustrating, and it makes comparing data cumbersome. In
        PyARPES x,y,z is always the total inferrable coordinate value,
        i.e. (+/- long range +/- high resolution) as appropriate. You can still access the
        underlying coordinates in this case as `long_{dim}` and `short_{dim}`.

        Args:
            data: The input dataset to adjust coordinates for.

        Returns:
            The updated data.
        """
        for d_name in ["x", "y", "z"]:
            short, long = f"short_{d_name}", f"long_{d_name}"
            phys = f"physical_long_{d_name}"

            def lookup(name: str):
                coordinate = data.S.lookup_coord(name)
                try:
                    return coordinate.values
                except AttributeError:
                    return coordinate

            c_short, c_long = lookup(short), lookup(long)

            data.coords[phys] = -data.coords[phys]

            scan_coord_name = None
            if isinstance(c_short, np.ndarray):
                scan_coord_name = short
            elif isinstance(c_long, np.ndarray):
                scan_coord_name = long

            if scan_coord_name:
                data = data.rename({scan_coord_name: d_name})
                data = data.assign_coords(
                    **{
                        d_name: -c_short - c_long,
                        scan_coord_name: -c_short if scan_coord_name == short else -c_long,
                    },
                )
            else:
                data = data.assign_coords(**{d_name: -c_short - c_long})

        return data

    @staticmethod
    def unwind_serptentine(data: xr.Dataset) -> xr.Dataset:
        """Changes serpentine scan data to a standard x-y cartesian scan format.

        MAESTRO supports a serpentine (think snake the computer game) scan mode to minimize the
        motion time for coarsely locating samples. Unfortunately, the DAQ just dumps the raw data,
        so we have to unwind it ourselves.

        Args:
            data: The data to be normalized

        Returns:
            Data after conversion to standard x-y cartesian coordinates.
        """
        spectra = data.S.spectra
        for spectrum in spectra:
            # serpentine axis always seems to be the first one, thankfully
            old_values = spectrum.values.copy()

            mask = np.mod(np.array(range(len(spectrum.coords[spectrum.dims[0]]))), 2) == 1
            old_values[mask] = np.roll(old_values[mask, ::-1], 2, axis=1)

            spectrum.values = old_values

        return data

    def postprocess_final(self, data: xr.Dataset, scan_desc: ScanDesc | None = None):
        """Perform final preprocessing of MAESTRO nano-ARPES data.

        In addition to standard tasks, we need to build a single unified spatial coordinate
        system. nano-ARPES at MAESTRO uses a coarse and fine (piezo slip-stick/inertial) motion
        control which are called "long_x"/"long_y"/"long_z" and "short_x"/"short_y"/"short_z"
        respectively.

        The physical motors use

        By convention, "x"/"y"/"z" are the canonical spatial coordinates, so we convert the true
        ("long_x" +/- "short_x") location to millimeters and make this the final coordinate.

        You can look at `MAESTRONanoARPESEndstation.update_hierarchical_coordinates` for details.

        Additionally, we do some normalization of different scan modes offered on this beamline,
        like "serpentine" (x-y zigzag) scanning.
        """
        data = data.rename({k: v for k, v in self.RENAME_COORDS.items() if k in data.coords})
        data = super().postprocess_final(data, scan_desc)

        # microns to mm
        ls = [data, *data.S.spectra]
        for c in [
            "short_x",
            "short_y",
            "short_z",
            "physical_long_x",
            "physical_long_y",
            "physical_long_z",
        ]:
            for a_data in ls:
                if c in a_data.attrs:
                    a_data.attrs[c] = a_data.attrs[c] / 1000

                a_data.coords[c] = a_data.coords[c] / 1000

        data = MAESTRONanoARPESEndstation.update_hierarchical_coordinates(data)
        if data.attrs["daq_type"] == "MotorSerpentine":
            data = MAESTRONanoARPESEndstation.unwind_serptentine(data)

        # we return new data from update_hierarchical, so we need to refresh
        # the definition of ls
        ls = [data, *data.S.spectra]
        for a_data in ls:
            a_data.coords["alpha"] = np.pi / 2
            a_data.attrs["alpha"] = np.pi / 2

            a_data.attrs["phi_offset"] = 0.4

            a_data.coords["phi"] = a_data.coords["phi"] / 2

        for deg_to_rad_coord in ["theta", "psi", "beta"]:
            for a_data in ls:
                a_data.coords[deg_to_rad_coord] = np.deg2rad(a_data.coords[deg_to_rad_coord])
                if deg_to_rad_coord in a_data.attrs:
                    a_data.attrs[deg_to_rad_coord] = np.deg2rad(a_data.attrs[deg_to_rad_coord])

        return data