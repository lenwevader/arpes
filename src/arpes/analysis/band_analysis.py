"""Provides some band analysis tools."""

from __future__ import annotations

import contextlib
import copy
import functools
import itertools
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import xarray as xr
from scipy.spatial import distance

import arpes.models.band
import arpes.utilities.math
from arpes.constants import HBAR_SQ_EV_PER_ELECTRON_MASS_ANGSTROM_SQ, TWO_DIMENSION
from arpes.fits import AffineBackgroundModel, LorentzianModel, QuadraticModel, broadcast_model
from arpes.provenance import update_provenance
from arpes.utilities import enumerate_dataarray
from arpes.utilities.conversion.forward import convert_coordinates_to_kspace_forward
from arpes.utilities.jupyter import wrap_tqdm

if TYPE_CHECKING:
    from collections.abc import Generator

    import lmfit as lf
    from _typeshed import Incomplete
    from numpy.typing import NDArray

    from arpes._typing import XrTypes

__all__ = (
    "fit_bands",
    "fit_for_effective_mass",
)


def fit_for_effective_mass(
    data: xr.DataArray,
    fit_kwargs: dict | None = None,
) -> float:
    """Fits for the effective mass in a piece of data.

    Performs an effective mass fit by first fitting for Lorentzian lineshapes and then fitting
    a quadratic model to the result. This is an alternative to global effective mass fitting.

    In the case that data is provided in anglespace, the Lorentzian fits are performed in anglespace
    before being converted to momentum where the effective mass is extracted.

    We should probably include uncertainties here.

    Args:
        data (DataType): ARPES data
        fit_kwargs: Passthrough for arguments to `broadcast_model`, used internally to
          obtain the Lorentzian peak locations

    Returns:
        The effective mass in units of the bare mass.
    """
    if fit_kwargs is None:
        fit_kwargs = {}

    mom_dim = next(
        dim for dim in ["kp", "kx", "ky", "kz", "phi", "beta", "theta"] if dim in data.dims
    )

    results = broadcast_model(
        [LorentzianModel, AffineBackgroundModel],
        data=data,
        broadcast_dims=mom_dim,
        **fit_kwargs,
    )
    if mom_dim in {"phi", "beta", "theta"}:
        forward = convert_coordinates_to_kspace_forward(data)
        assert isinstance(forward, xr.Dataset)
        final_mom = next(dim for dim in ["kx", "ky", "kp", "kz"] if dim in forward)
        eVs = results.F.p("a_center").values
        kps = [
            forward[final_mom].sel({mom_dim: ang}, eV=eV, method="nearest")
            for eV, ang in zip(eVs, data.coords[mom_dim].values, strict=True)
        ]
        quad_fit = QuadraticModel().fit(eVs, x=np.array(kps))

        return HBAR_SQ_EV_PER_ELECTRON_MASS_ANGSTROM_SQ / (2 * quad_fit.params["a"].value)

    quad_fit = QuadraticModel().guess_fit(results.F.p("a_center"))
    return HBAR_SQ_EV_PER_ELECTRON_MASS_ANGSTROM_SQ / (2 * quad_fit.params["a"].value)


def unpack_bands_from_fit(
    band_results: xr.DataArray,
    weights: tuple[float, float, float] = (2, 0, 10),
) -> list[arpes.models.band.Band]:
    """Deconvolve the band identities of a series of overlapping bands.

    Sometimes through the fitting process, or across a place in the band structure where there is a
    nodal point, the identities of the bands across sequential fits can get mixed up.

    We can try to restore this identity by using the cosine similarity of fits, where the fit is
    represented as a vector by:

        v_band =  (sigma, amplitude, center) * weights
        weights = (5, 1/5, 10)

    For any point in the band structure, we find the closest place where we have fixed the band
    identities. Let the bands be indexed by i so that the bands are b_i and b_i_0 at the point of
    interest and at the reference respectively.

    Then, we calculate the matrix:
        s_ij = sim(b_i, b_j_0)

    The band identities are subsequently chosen so that the trace of this matrix is maximized among
    possible ways of labelling the bands b_i.

    The value of the weights parameter is chosen only to scale the dimensions so that they are
    closer to the same magnitude.

    Args:
        arr
        band_results (xr.DataArray): band results.
        weights (tuple[float, float, float]): weight values for sigma, amplitude, center

    Returns:
        Unpacked bands.
    """
    template_components = band_results.values[0].model.components
    prefixes = [component.prefix for component in template_components]

    identified_band_results = copy.deepcopy(band_results)

    def as_vector(model_fit: lf.ModelResult, prefix: str = "") -> NDArray[np.float_]:
        """Convert lf.ModelResult to NDArray.

        Args:
            model_fit ([TODO:type]): [TODO:description]
            prefix ([TODO:type]): [TODO:description]
        """
        stderr = np.array(
            [
                model_fit.params[prefix + "sigma"].stderr,
                model_fit.params[prefix + "amplitude"].stderr,
                model_fit.params[prefix + "center"].stderr,
            ],
        )
        return (
            np.array(
                [
                    model_fit.params[prefix + "sigma"].value,
                    model_fit.params[prefix + "amplitude"].value,
                    model_fit.params[prefix + "center"].value,
                ],
            )
            * weights
            / (1 + stderr)
        )

    identified_by_coordinate = {}
    first_coordinate = None
    for coordinate, fit_result in enumerate_dataarray(band_results):
        frozen_coord = tuple(coordinate[d] for d in band_results.dims)

        closest_identified = None
        dist = float("inf")
        for coord, identified_band in identified_by_coordinate.items():
            current_dist = np.dot(coord, frozen_coord)
            if current_dist < dist:
                closest_identified = identified_band
                dist = current_dist

        if closest_identified is None:
            first_coordinate = coordinate
            closest_identified = [c.prefix for c in fit_result.model.components], fit_result
            identified_by_coordinate[frozen_coord] = closest_identified

        closest_prefixes, closest_fit = closest_identified
        mat_shape = (
            len(prefixes),
            len(prefixes),
        )
        dist_mat = np.zeros(shape=mat_shape)

        for i, j in np.ndindex(mat_shape):
            dist_mat[i, j] = distance.euclidean(
                as_vector(fit_result, prefixes[i]),
                as_vector(closest_fit, closest_prefixes[j]),
            )

        best_arrangement: tuple[int, ...] = tuple(range(len(prefixes)))
        best_trace = float("inf")
        for p in itertools.permutations(range(len(prefixes))):
            trace = sum(dist_mat[i, p_i] for i, p_i in enumerate(p))
            if trace < best_trace:
                best_trace = trace
                best_arrangement = p
        ordered_prefixes = [closest_prefixes[p_i] for p_i in best_arrangement]
        identified_by_coordinate[frozen_coord] = ordered_prefixes, fit_result
        identified_band_results.loc[coordinate] = ordered_prefixes

    # Now that we have identified the bands,
    # extract them into real bands
    bands = []
    for i in range(len(prefixes)):
        label = identified_band_results.loc[first_coordinate].values.item()[i]

        def dataarray_for_value(param_name: str, i: int = i, *, is_value: bool) -> xr.DataArray:
            """[TODO:summary].

            Args:
                param_name (str): [TODO:description]
                i (int): [TODO:description]
                is_value (bool): [TODO:description]
            """
            values: NDArray[np.float_] = np.ndarray(
                shape=identified_band_results.values.shape,
                dtype=float,
            )
            it = np.nditer(values, flags=["multi_index"], op_flags=[["writeonly"]])
            while not it.finished:
                prefix = identified_band_results.values[it.multi_index][i]
                param = band_results.values[it.multi_index].params[prefix + param_name]
                if is_value:
                    it[0] = param.value
                else:
                    it[0] = param.stderr
                it.iternext()

            return xr.DataArray(
                values,
                identified_band_results.coords,
                identified_band_results.dims,
            )

        band_data = xr.Dataset(
            {
                "center": dataarray_for_value("center", is_value=True),
                "center_stderr": dataarray_for_value("center", is_value=False),
                "amplitude": dataarray_for_value("amplitude", is_value=True),
                "amplitude_stderr": dataarray_for_value("amplitude", is_value=False),
                "sigma": dataarray_for_value("sigma", is_value=True),
                "sigma_stderr": dataarray_for_value("sigma", is_value=False),
            },
        )
        bands.append(arpes.models.band.Band(label, data=band_data))

    return bands


@update_provenance("Fit bands from pattern")
def fit_patterned_bands(
    arr: xr.DataArray,
    band_set: dict[Incomplete, Incomplete],
    fit_direction: str = "",
    stray: float | None = None,
    *,
    background: bool = True,
    interactive: bool = True,
    dataset: bool = True,
) -> XrTypes:
    """Fits bands and determines dispersion in some region of a spectrum.

    The dimensions of the dataset are partitioned into three types:

    1. Fit directions, these are coordinates along the 1D (or maybe later 2D) marginals
    2. Broadcast directions, these are directions used to interpolate against the patterned
       directions
    3. Free directions, these are broadcasted but they are not used to extract initial values of the
       fit parameters

    For instance, if you laid out band patterns in a E, k_p, delay spectrum at delta_t=0, then if
    you are using MDCs, k_p is the fit direction, E is the broadcast direction, and delay is a free
    direction.

    In general we can recover the free directions and the broadcast directions implicitly by
    examining the band_set passed as a pattern.

    Args:
        arr (xr.DataArray):
        band_set: dictionary with bands and points along the spectrum
        fit_direction (str):
        stray (float, optional):
        orientation: edc or mdc
        direction_normal
        preferred_k_direction
        dataset: if True, return as Dataset

    Returns:
        Dataset or DataArray, as controlled by the parameter "dataset"
    """
    if background:
        from arpes.models.band import AffineBackgroundBand

        background = AffineBackgroundBand

    free_directions = list(arr.dims)
    free_directions.remove(fit_direction)

    def resolve_partial_bands_from_description(
        coord_dict: dict[str, Incomplete],
        name: str = "",
        band: Incomplete = None,
        dims: list[str] | tuple[str, ...] | None = None,
        params: Incomplete = None,
        points: Incomplete = None,
        marginal: Incomplete = None,
    ) -> list[dict[str, Any]]:
        # You don't need to supply a marginal, but it is useful because it allows estimation of the
        # initial value for the amplitude from the approximate peak location

        if params is None:
            params = {}
        if dims is None:
            dims = ()

        coord_name = next(d for d in dims if d in coord_dict)
        partial_band_locations = list(
            _interpolate_intersecting_fragments(
                coord_dict[coord_name],
                arr.dims.index(coord_name),
                points or [],
            ),
        )

        return [
            {
                "band": band,
                "name": f"{name}_{i}",
                "params": _build_params(
                    params=params,
                    center=band_center,
                    center_stray=params.get("stray", stray),
                    marginal=marginal,
                ),
            }
            for i, (_, band_center) in enumerate(partial_band_locations)
        ]

    template = arr.sum(fit_direction)
    band_results = xr.DataArray(
        np.ndarray(shape=template.values.shape, dtype=object),
        coords=template.coords,
        dims=template.dims,
        attrs=template.attrs,
    )

    total_slices = np.prod([len(arr.coords[d]) for d in free_directions])
    for coord_dict, marginal in wrap_tqdm(
        arr.G.iterate_axis(free_directions),
        interactive=interactive,
        desc="fitting",  # Prefix for the progressbar.
        total=total_slices,  # The number of expected iterations. If unspecified,
    ):
        partial_bands = [
            resolve_partial_bands_from_description(
                coord_dict=coord_dict,
                marginal=marginal,
                **band_set_values,
            )
            for band_set_values in band_set.values()
        ]

        partial_bands = [p for p in partial_bands if len(p)]

        if background is not None and partial_bands:
            partial_bands = [*partial_bands, [{"band": background, "name": "", "params": {}}]]

        internal_models = [_instantiate_band(b) for bs in partial_bands for b in bs]

        if not internal_models:
            band_results.loc[coord_dict] = None
            continue

        composite_model = functools.reduce(lambda x, y: x + y, internal_models)
        new_params = composite_model.make_params()
        fit_result = composite_model.fit(
            marginal.values,
            new_params,
            x=marginal.coords[next(iter(marginal.indexes))].values,
        )

        # populate models, sample code
        band_results.loc[coord_dict] = fit_result

    if not dataset:
        band_results.attrs["original_data"] = arr
        return band_results

    residual = arr.copy(deep=True)
    residual.values = np.zeros(residual.shape)

    for coords in band_results.G.iter_coords():
        fit_item = band_results.sel(coords).item()
        if fit_item is None:
            continue

        with contextlib.suppress(Exception):
            residual.loc[coords] = fit_item.residual

    return xr.Dataset(
        {
            "data": arr,
            "residual": residual,
            "results": band_results,
            "norm_residual": residual / arr,
        },
        residual.coords,
    )


def _is_between(x: float, y0: float, y1: float) -> bool:
    y0, y1 = np.min([y0, y1]), np.max([y0, y1])
    return y0 <= x <= y1


def _instantiate_band(partial_band: dict[str, Any]) -> lf.Model:
    phony_band = partial_band["band"](partial_band["name"])
    built = phony_band.fit_cls(prefix=partial_band["name"], missing="drop")
    for constraint_coord, params in partial_band["params"].items():
        if constraint_coord == "stray":
            continue
        built.set_param_hint(constraint_coord, **params)
    return built


def fit_bands(
    arr: xr.DataArray,
    band_description: Incomplete,
    direction: Literal["edc", "mdc", "EDC", "MDC"] = "mdc",
    preferred_k_direction: str = "",
    step: Literal["initial", None] = None,
) -> tuple[xr.DataArray | None, None, lf.ModelResult | None]:
    """Fits bands and determines dispersion in some region of a spectrum.

    Args:
        arr(xr.DataArray):
        band_description: A description of the bands to fit in the region
        background
        direction

    Returns:
        Fitted bands.
    """
    assert direction in ["edc", "mdc", "EDC", "MDC"]

    directions = list(arr.dims)

    broadcast_direction = "eV"

    if (
        direction == "mdc" and not preferred_k_direction
    ):  # TODO: Need to check (Is preferred_k_direction is required?)
        possible_directions = set(directions).intersection({"kp", "kx", "ky", "phi"})
        broadcast_direction = next(iter(possible_directions))

    directions.remove(broadcast_direction)

    residual, _ = next(_iterate_marginals(arr, directions))
    residual = residual - np.min(residual.values)

    # Let the first band be given by fitting the raw data to this band
    # Find subsequent peaks by fitting models to the residuals
    raw_bands = [band.get("band") if isinstance(band, dict) else band for band in band_description]
    initial_fits = None
    all_fit_parameters = {}

    if step == "initial":
        residual.plot()

    for band in band_description:
        if isinstance(band, dict):
            band_inst = band.get("band")
            params = band.get("params", {})
        else:
            band_inst = band
            params = None
        fit_model = band_inst.fit_cls(prefix=band_inst.label)
        initial_fit = fit_model.guess_fit(residual, params=params)
        if initial_fits is None:
            initial_fits = initial_fit.params
        else:
            initial_fits.update(initial_fit.params)

        residual = residual - initial_fit.best_fit
        if isinstance(band_inst, arpes.models.band.BackgroundBand):
            # This is an approximation to simulate a constant background band underneath the data
            # Because backgrounds are added to our model only after the initial sequence of fits.
            # This is by no means the most appropriate way to do this, just one that works
            # alright for now
            pass

        if step == "initial":
            residual.plot()
            (residual - residual + initial_fit.best_fit).plot()

    if step == "initial":
        return None, None, residual

    template = arr.sum(broadcast_direction)
    band_results = xr.DataArray(
        np.ndarray(shape=template.values.shape, dtype=object),
        coords=template.coords,
        dims=template.dims,
        attrs=template.attrs,
    )

    for marginal, coordinate in _iterate_marginals(arr, directions):
        # Use the closest parameters that have been successfully fit, or use the initial
        # parameters, this should be good enough because the order of the iterator will
        # be stable
        closest_model_params = initial_fits  # fix me
        dist = float("inf")
        frozen_coordinate = tuple(coordinate[k] for k in template.dims)
        for c, v in all_fit_parameters.items():
            delta = np.array(c) - frozen_coordinate
            current_distance = delta.dot(delta)
            if current_distance < dist and direction == "mdc":  # TODO: remove me
                closest_model_params = v

        # TODO: mix in any params to the model params

        # populate models
        internal_models = [band.fit_cls(prefix=band.label) for band in raw_bands]
        composite_model = functools.reduce(lambda x, y: x + y, internal_models)
        new_params = composite_model.make_params(
            **{k: v.value for k, v in closest_model_params.items()},
        )
        fit_result = composite_model.fit(
            marginal.values,
            new_params,
            x=marginal.coords[next(iter(marginal.indexes))].values,
        )

        # insert fit into the results, insert the parameters into the cache so that we have
        # fitting parameters for the next sequence
        band_results.loc[coordinate] = fit_result
        all_fit_parameters[frozen_coordinate] = fit_result.params

    # Unpack the band results
    unpacked_bands = None
    residual = None

    return band_results, unpacked_bands, residual  # Memo bunt_result is xr.DataArray


def _interpolate_intersecting_fragments(coord, coord_index, points):
    """Finds all consecutive pairs of points in `points`.

    [TODO:description]

    Args:
        coord ([TODO:type]): [TODO:description]
        coord_index ([TODO:type]): [TODO:description]
        points ([TODO:type]): [TODO:description]
    """
    assert len(points[0]) == TWO_DIMENSION

    for point_low, point_high in pairwise(points):
        coord_other_index = 1 - coord_index

        check_coord_low, check_coord_high = point_low[coord_index], point_high[coord_index]
        if _is_between(coord, check_coord_low, check_coord_high):
            # this is unnecessarily complicated
            if check_coord_low < check_coord_high:
                yield (
                    coord,
                    (coord - check_coord_low)
                    / (check_coord_high - check_coord_low)
                    * (point_high[coord_other_index] - point_low[coord_other_index])
                    + point_low[coord_other_index],
                )
            else:
                yield (
                    coord,
                    (coord - check_coord_high)
                    / (check_coord_low - check_coord_high)
                    * (point_low[coord_other_index] - point_high[coord_other_index])
                    + point_high[coord_other_index],
                )


def _iterate_marginals(
    arr: xr.DataArray,
    iterate_directions: list[str] | None = None,
) -> Generator[tuple[xr.DataArray, dict[str, Any], None, None]]:
    if iterate_directions is None:
        iterate_directions = [str(dim) for dim in arr.dims]
        iterate_directions.remove("eV")

    selectors = itertools.product(*[arr.coords[d] for d in iterate_directions])
    for ss in selectors:
        coords = dict(zip(iterate_directions, [float(s) for s in ss], strict=True))
        yield arr.sel(coords), coords


def _build_params(
    params: dict[str, Any],
    center: float,
    center_stray: float | None = None,
    marginal: xr.DataArray | None = None,
) -> dict[str, Any]:
    params.update(
        {
            "center": {
                "value": center,
            },
        },
    )
    if center_stray is not None:
        params["center"]["min"] = center - center_stray
        params["center"]["max"] = center + center_stray
        params["sigma"] = params.get("sigma", {})
        params["sigma"]["value"] = center_stray
        if marginal is not None:
            near_center = marginal.sel(
                {
                    marginal.dims[0]: slice(
                        center - 1.2 * center_stray,
                        center + 1.2 * center_stray,
                    ),
                },
            )
            low, high = np.percentile(
                near_center.values,
                (20, 80),
            )
            params["amplitude"] = params.get("amplitude", {})
            params["amplitude"]["value"] = high - low
    return params