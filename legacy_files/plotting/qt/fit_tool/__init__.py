"""Provides a Qt based implementation of a curve fit inspection tool."""

from __future__ import annotations

import contextlib
import enum
import warnings
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING

import dill
import matplotlib as mpl
import numpy as np
import pyqtgraph as pg
import xarray as xr
from arpes.constants import TWO_DIMENSION
from arpes.fits.utilities import result_to_hints
from arpes.plotting.qt.qt_tool.BinningInfoWidget import BinningInfoWidget
from arpes.utilities.qt import (
    BasicHelpDialog,
    DataArrayImageView,
    SimpleApp,
    SimpleWindow,
    qt_info,
    run_tool_in_daemon_process,
)
from arpes.utilities.qt.data_array_image_view import DataArrayPlot
from arpes.utilities.qt.utils import PlotOrientation, ReactivePlotRecord
from arpes.utilities.ui import CursorRegion, KeyBinding, button, horizontal, label, tabs
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtWidgets import QLayout, QWidget

from .fit_inspection_plot import FitInspectionPlot

if TYPE_CHECKING:
    from collections.abc import Iterable

    from _typeshed import Incomplete

__all__ = (
    "FitTool",
    "fit_tool",
)

qt_info.setup_pyqtgraph()


class DataKey(enum.StrEnum):
    Data = "data"
    Residual = "residual"
    NormalizedResidual = "norm_residual"


class FitToolWindow(SimpleWindow):
    """The application window for `FitTool`."""

    HELP_DIALOG_CLS = BasicHelpDialog

    def compile_key_bindings(self) -> list[KeyBinding]:
        return [
            *super().compile_key_bindings(),
            KeyBinding(
                "Scroll Cursor",
                [
                    QtCore.Qt.Key.Key_Left,
                    QtCore.Qt.Key.Key_Right,
                    QtCore.Qt.Key.Key_Up,
                    QtCore.Qt.Key.Key_Down,
                ],
                self.scroll,
            ),
            KeyBinding("Reset Intensity", [QtCore.Qt.Key.Key_I], self.reset_intensity),
            KeyBinding("Center Cursor", [QtCore.Qt.Key.Key_C], self.center_cursor),
            KeyBinding("Transpose - Roll Axis", [QtCore.Qt.Key.Key_T], self.transpose_roll),
            KeyBinding("Transpose - Swap Front Axes", [QtCore.Qt.Key.Key_Y], self.transpose_swap),
        ]

    def center_cursor(self) -> None:
        self.app().center_cursor()

    def transpose_roll(self) -> None:
        self.app().transpose_to_front(-1)

    def transpose_swap(self) -> None:
        self.app().transpose_to_front(1)

    @staticmethod
    def _update_scroll_delta(delta: tuple[int, int], event: QtGui.QKeyEvent) -> tuple[int, int]:
        if event.nativeModifiers() & 1:  # shift key
            delta = (delta[0], delta[1] * 5)

        if event.nativeModifiers() & 2:  # shift key
            delta = (delta[0], delta[1] * 20)

        return delta

    def reset_intensity(self) -> None:
        self.app().reset_intensity()

    def scroll_z(self, event: QtGui.QKeyEvent) -> None:
        key_map = {
            QtCore.Qt.Key.Key_N: (2, -1),
            QtCore.Qt.Key.Key_M: (2, 1),
        }

        delta = self._update_scroll_delta(key_map.get(event.key()), event)

        if delta is not None and self.app() is not None:
            self.app().scroll(delta)

    def scroll(self, event: QtGui.QKeyEvent) -> None:
        key_map = {
            QtCore.Qt.Key.Key_Left: (0, -1),
            QtCore.Qt.Key.Key_Right: (0, 1),
            QtCore.Qt.Key.Key_Down: (1, -1),
            QtCore.Qt.Key.Key_Up: (1, 1),
        }

        delta = self._update_scroll_delta(key_map.get(event.key(), (0, 0)), event)

        if delta is not None and self.app() is not None:
            self.app().scroll(delta)


@dataclass
class FitTool(SimpleApp):
    """FitTool is an implementation of a curve fit browser for PyARPES."""

    data_key: DataKey = DataKey.Data
    dataset: xr.Dataset | None = None

    TITLE = "Fit Tool"
    WINDOW_CLS = FitToolWindow
    WINDOW_SIZE = (8, 5)

    def __init__(self) -> None:
        """Initialize attributes to safe empty values."""
        super().__init__()

        self.content_layout = None
        self.main_layout = None

    @property
    def data(self) -> xr.Dataset:
        """Extract the array-like values according to what content we are rendering."""
        return self.dataset[self.data_key.value]

    @data.setter
    def data(self, new_data: xr.DataArray) -> None:
        del new_data
        msg = "On fit_tool, the data is computed from the original dataset."
        raise TypeError(msg)

    def center_cursor(self) -> None:
        """Scrolls so that the cursors are in the center of the data volume."""
        new_cursor = [len(self.data.coords[d]) / 2 for d in self.data.dims]
        self.update_cursor_position(new_cursor)

        for i, cursors in self.registered_cursors.items():
            for cursor in cursors:
                cursor.set_location(new_cursor[i])

    def scroll(self, delta: Iterable[float]) -> None:
        """Scroll the axis delta[0] by delta[1] pixels."""
        if delta[0] >= len(self.context["cursor"]):
            warnings.warn("Tried to scroll a non-existent dimension.", stacklevel=2)
            return

        cursor = list(self.context["cursor"])
        cursor[delta[0]] += delta[1]

        self.update_cursor_position(cursor)

        for i, cursors in self.registered_cursors.items():
            for c in cursors:
                c.set_location(cursor[i])

    def transpose(self, transpose_order: list[str]) -> None:
        """Transpose dimensions into the order specified by `transpose_order` and redraw."""
        reindex_order = [self.data.dims.index(t) for t in transpose_order]
        self.data = self.data.transpose(*transpose_order)

        new_cursor = [self.context["cursor"][i] for i in reindex_order]
        self.update_cursor_position(new_cursor, force=True)

        for i, cursors in self.registered_cursors.items():
            for cursor in cursors:
                cursor.set_location(new_cursor[i])

    def transpose_to_front(self, dim: str | int) -> None:
        """Transpose the dimension `dim` to the front so that it is in the main marginal."""
        if not isinstance(dim, str):
            dim = tuple(self.data.dims).index(dim)

        order = list(self.data.dims)
        order.remove(dim)
        order = [dim, *order]
        self.transpose(order)

    def configure_image_widgets(self) -> None:
        """Configure array marginals for the input data.

        Depending on the array dimensionality, we need a different number and variety
        of marginals. This is as easy as specifying which marginals we select over and
        handling the rest dynamically.

        An additional complexity is that we also handle the cursor registration here.

        Unlike the simple data browser, we have a few additional complexities. For one,
        we need to generate a display of the fit which is currently under the cursor.

        For now, we are going to support 1D fits only. The remaining data will be either
        1, 2, or 3 dimensional, under current assumptions. Let's consider each case in
        turn.

        One Dimensional Marginals:
            In this case, the total dataset is 2D, we will put a single 1D cursor
            on the broadcast axis but we will display *all* of the data as a 2D image
            plot with a 1D marginal and the 1D fit display.

        Two Dimensional Marginals:
            Here, we will give a single 2D image plot with the perpendicular (fit)
            axis out of the image plane. A cursor will be registered to the 1D fit
            display so that the contents of the main display can be changed. This 1D
            fit cursor will have binning controls.

        Three Dimensional Marginals:
            The total dataset is 4D. We will use the standard 3D set of marginal planes,
            and 1D marginals like we do in the 3D qt_tool with a 3D cursor.

            The 1D marginal will have a cursor and binning controls on that cursor.
        """
        if len(self.data.dims) == TWO_DIMENSION:
            self.generate_marginal_for((), 0, 0, "xy", cursors=True, layout=self.content_layout)
            self.generate_fit_marginal_for(
                (0, 1),
                (0, 1),
                "fit",
                cursors=False,
                orientation=PlotOrientation.Vertical,
                layout=self.content_layout,
            )
            self.views["xy"].view.setYLink(self.views["fit"].inner_plot)

        if len(self.data.dims) == TWO_DIMENSION + 1:
            self.generate_marginal_for((2,), 1, 0, "xy", cursors=True, layout=self.content_layout)
            self.generate_fit_marginal_for(
                (0, 1, 2),
                (0, 0),
                "fit",
                cursors=True,
                layout=self.content_layout,
            )

        if len(self.data.dims) == 4:  # noqa: PLR2004
            # no idea if these marginal locations are correct, need to check that
            self.generate_marginal_for((1, 3), 1, 0, "xz", cursors=True, layout=self.content_layout)
            self.generate_marginal_for((2, 3), 0, 1, "xy", cursors=True, layout=self.content_layout)
            self.generate_marginal_for((0, 3), 1, 1, "yz", layout=self.content_layout)
            self.generate_fit_marginal_for(
                (0, 1, 2, 3),
                (0, 0),
                "fit",
                cursors=True,
                layout=self.content_layout,
            )

    def generate_fit_marginal_for(  # noqa: PLR0913
        self,
        dimensions: tuple[int, ...],
        column_row: tuple[int, int],
        name: str = "fit",
        orientation: PlotOrientation = PlotOrientation.Horizontal,
        *,
        cursors: bool = False,
        layout: QLayout | None = None,
    ) -> FitInspectionPlot:
        """Generates a marginal plot for a fit at a given set of coordinates.

        This does something very similar to `generate_marginal_for` except that it is
        specialized to showing a widget which embeds information about the current fit result.
        """
        column, row = column_row
        if layout is None:
            layout = self._layout
        assert isinstance(layout, QLayout)

        remaining_dims = [_ for _ in list(range(len(self.data.dims))) if _ not in dimensions]

        # for now, we only allow a single fit dimension
        widget = FitInspectionPlot(name=name, root=weakref.ref(self), orientation=orientation)
        self.views[name] = widget

        if orientation == PlotOrientation.Horizontal:
            widget.setMaximumHeight(qt_info.inches_to_px(3))
        else:
            widget.setMaximumWidth(qt_info.inches_to_px(3))

        if cursors:
            cursor = CursorRegion(
                orientation=(
                    CursorRegion.Vertical
                    if orientation == PlotOrientation.Vertical
                    else CursorRegion.Horizontal
                ),
                movable=True,
            )
            widget.addItem(cursor, ignoreBounds=False)
            self.connect_cursor(remaining_dims[-1], cursor)

        self.reactive_views.append(
            ReactivePlotRecord(dims=dimensions, view=widget, orientation=orientation),
        )
        layout.addWidget(widget, column, row)
        return widget

    def update_cursor_position(  # noqa: C901  # Might be removed in the future.
        self,
        new_cursor: list[float],
        *,
        force: bool = False,
        keep_levels: bool = True,
    ) -> None:
        """Sets the current cursor position.

        Because setting the cursor position changes the marginal data, this is also
        where redrawing originates.

        The way we do this is basically to step through views, recompute the slice for that view
        and set the image/array on the slice.
        """
        old_cursor = list(self.context["cursor"])
        self.context["cursor"] = new_cursor

        def index_to_value(index: int, dim: str) -> float:
            d = self.data.dims[dim]
            c = self.data.coords[d].values
            return c[0] + index * (c[1] - c[0])

        self.context["value_cursor"] = [index_to_value(v, i) for i, v in enumerate(new_cursor)]

        changed_dimensions = [
            i for i, (x, y) in enumerate(zip(old_cursor, new_cursor, strict=True)) if x != y
        ]

        cursor_text = ",".join(
            f"{x}: {y:.4g}"
            for x, y in zip(self.data.dims, self.context["value_cursor"], strict=True)
        )
        self.window.statusBar().showMessage(f"({cursor_text})")

        # update data
        def safe_slice(vlow: float, vhigh: float, axis: int = 0) -> slice:
            vlow, vhigh = int(min(vlow, vhigh)), int(max(vlow, vhigh))
            rng = len(self.data.coords[self.data.dims[axis]])
            vlow, vhigh = np.clip(vlow, 0, rng), np.clip(vhigh, 0, rng)

            if vlow == vhigh:
                vhigh = vlow + 1

            vlow, vhigh = np.clip(vlow, 0, rng), np.clip(vhigh, 0, rng)

            if vlow == vhigh:
                vlow = vhigh - 1

            return slice(vlow, vhigh)

        for reactive in self.reactive_views:
            if set(reactive.dims).intersection(set(changed_dimensions)) or force:
                try:
                    select_coord = dict(
                        zip(
                            [self.data.dims[i] for i in reactive.dims],
                            [
                                safe_slice(int(new_cursor[i]), int(new_cursor[i] + 1), i)
                                for i in reactive.dims
                            ],
                            strict=True,
                        ),
                    )
                    assert isinstance(self.dataset, xr.Dataset)
                    if isinstance(reactive.view, DataArrayImageView):
                        image_data = self.data.isel(select_coord)
                        if select_coord:
                            image_data = image_data.mean(list(select_coord.keys()))
                        reactive.view.setImage(image_data, keep_levels=keep_levels)
                    elif isinstance(reactive.view, FitInspectionPlot):
                        results_coord = {
                            k: v for k, v in select_coord.items() if k in self.dataset.results.dims
                        }
                        result = self.dataset.results.isel(results_coord)
                        result = result.item()
                        reactive.view.set_model_result(result)

                    elif isinstance(reactive.view, pg.PlotWidget):
                        for_plot = self.data.isel(select_coord)
                        if select_coord:
                            for_plot = for_plot.mean(list(select_coord.keys()))

                        cursors = [
                            _
                            for _ in reactive.view.getPlotItem().items
                            if isinstance(_, CursorRegion)
                        ]
                        reactive.view.clear()
                        for c in cursors:
                            reactive.view.addItem(c)

                        if isinstance(reactive.view, DataArrayPlot):
                            reactive.view.plot(for_plot)
                            continue

                        if reactive.orientation == PlotOrientation.Horizontal:
                            reactive.view.plot(for_plot.values)
                        else:
                            reactive.view.plot(for_plot.values, range(len(for_plot.values)))
                except IndexError:
                    pass

    def construct_binning_tab(self) -> QWidget:
        """Gives tab controls for the axis along the fit only."""
        inner_items = [
            BinningInfoWidget(axis_index=len(self.data.dims) - 1, root=weakref.ref(self)),
        ]
        return horizontal(label("Options"), *inner_items), inner_items

    def copy_parameter_hint(self, *_: Incomplete) -> None:
        """Converts parameters for the current model being displayed and copies to clipboard."""
        result = self.views["fit"].result
        hint = result_to_hints(result)
        self.copy_to_clipboard(hint)

    def construct_info_tab(self) -> QWidget:
        """Provides some utility functionality to make curve fitting easier."""
        copy_button = button("Copy parameters as hint")
        copy_button.setMaximumWidth(qt_info.inches_to_px(1.5))
        copy_button.subject.subscribe(self.copy_parameter_hint)
        inner_items = [copy_button]
        return horizontal(*inner_items), inner_items

    def add_contextual_widgets(self) -> None:
        """Adds the widgets for the contextual controls at the bottom."""
        assert isinstance(self.main_layout, QLayout)
        self.main_layout.addLayout(self.content_layout, 0, 0)
        info_tab, self.info_tab_widgets = self.construct_info_tab()
        binning_tab, self.binning_tab_widgets = self.construct_binning_tab()

        self.tabs = tabs(("Info", info_tab), ("Binning", binning_tab))
        self.tabs.setFixedHeight(qt_info.inches_to_px(1))

        self.main_layout.addLayout(self.content_layout, 0, 0)
        self.main_layout.addWidget(self.tabs, 1, 0)

    def layout(self) -> QLayout:
        """Initialize the layout components."""
        self.main_layout = QtWidgets.QGridLayout()
        self.content_layout = QtWidgets.QGridLayout()
        return self.main_layout

    def before_show(self) -> None:
        """Lifecycle hook for configuration before app show."""
        self.configure_image_widgets()
        self.add_contextual_widgets()

        self.set_colormap(mpl.colormaps["viridis"])

    def after_show(self) -> None:
        """Initialize application state after app show.

        To do this, we need to set the initial cursor location, and call update
        which forces a rerender.
        """
        # basic state initialization
        self.context.update(
            {
                "cursor": [self.data.coords[d].mean().item() for d in self.data.dims],
            },
        )

        # Display the data
        self.update_cursor_position(self.context["cursor"], force=True, keep_levels=False)
        self.center_cursor()

    def reset_intensity(self) -> None:
        """Autoscales intensity in each marginal plot."""
        self.update_cursor_position(self.context["cursor"], force=True, keep_levels=False)

    def set_data(self, data: xr.Dataset) -> None:
        """Sets the current data to a new value and resets UI state."""
        self.dataset = data
        self.data_key = DataKey.Data

        # For now, we only support 1D fit results
        fit_dims = self.dataset.F.fit_dimensions
        assert len(fit_dims) == 1
        self.dataset = self.dataset.S.transpose_to_back(*fit_dims)


def _fit_tool(data: xr.Dataset) -> None:
    """Starts the fitting inspection tool using an input fit result Dataset."""
    with contextlib.suppress(TypeError):
        data = dill.loads(data)  # noqa: S301

    # some sanity checks that we were actually passed a collection of fit results
    assert isinstance(data, xr.Dataset)
    assert "results" in data.data_vars

    tool = FitTool()
    tool.set_data(data)
    tool.start()


fit_tool = run_tool_in_daemon_process(_fit_tool)
