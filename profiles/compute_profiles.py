# Copyright (C) 2024 Andy Aschwanden, Constantine Khroulev
#
# This file is part of pypism.
#
# GLACIER-FLOW-TOOLS is free software; you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation; either version 3 of the License, or (at your option) any later
# version.
#
# GLACIER-FLOW-TOOLS is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License
# along with PISM; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""
Calculate proifles and compute statistics along profiles.
"""

import time
from argparse import Action, ArgumentDefaultsHelpFormatter, ArgumentParser
from functools import partial
from pathlib import Path
from typing import List

import dask_geopandas
import geopandas as gp
import xarray as xr
from dask import dataframe as dd
from dask.distributed import Client, LocalCluster, progress
from joblib import Parallel, delayed
from tqdm.auto import tqdm

from glacier_flow_tools.profiles import plot_glacier, plot_profile, process_profile
from glacier_flow_tools.utils import merge_on_intersection, preprocess_nc, tqdm_joblib


class ParseKwargs(Action):
    """
    Custom action for parsing keyword arguments from the command line.

    This class is used as an action within argparse to parse keyword arguments from the command line and store them in a dictionary.

    Methods
    -------
    __call__(parser, namespace, values, option_string=None):
        The method called when the action is triggered.

    Examples
    --------
    >>> parser = argparse.ArgumentParser()
    >>> parser.add_argument('--kwargs', nargs='*', action=ParseKwargs)
    >>> args = parser.parse_args('--kwargs key1=value1 key2=value2'.split())
    >>> print(args.kwargs)
    {'key1': 'value1', 'key2': 'value2'}
    """

    def __call__(self, parser, namespace, values, option_string=None):
        """
        Called when the action is triggered.

        This method is called when the action is triggered. It parses the keyword arguments from the command line and stores them in a dictionary.

        Parameters
        ----------
        parser : argparse.ArgumentParser
            The argument parser object.
        namespace : argparse.Namespace
            The namespace object that will be updated with the parsed values.
        values : list
            The command-line arguments to be parsed.
        option_string : str, optional
            The option string that was used to invoke this action.
        """
        setattr(namespace, self.dest, {})
        for value in values:
            key, value = value.split("=")
            getattr(namespace, self.dest)[key] = value


if __name__ == "__main__":

    # set up the option parser
    profiles_parser = ArgumentParser(formatter_class=ArgumentDefaultsHelpFormatter)
    profiles_parser.description = "Compute pathlines (forward/backward) given a velocity field (xr.Dataset) and starting points (geopandas.GeoDataFrame)."
    profiles_parser.add_argument(
        "--alpha",
        help="""Scale observational error. Use 0.05 to reproduce 'Commplex Outlet Glacier Flow Captured'. Default=0.""",
        default=0.0,
        type=float,
    )
    profiles_parser.add_argument(
        "--crs", help="""Coordinate reference system. Default is EPSG:3413.""", type=str, default="EPSG:3413"
    )
    profiles_parser.add_argument("--n_jobs", help="""Number of parallel jobs.""", type=int, default=4)
    profiles_parser.add_argument("--profiles_url", help="""Path to profiles.""", default=None, type=str)
    profiles_parser.add_argument(
        "--result_dir",
        help="""Path to where output is saved. Directory will be created if needed.""",
        default=Path("./results"),
    )
    profiles_parser.add_argument(
        "--sigma",
        help="""Sigma multiplier observational error. Default=1. (i.e. error is 1 standard deviation""",
        default=1.0,
        type=float,
    )
    profiles_parser.add_argument(
        "--segmentize", help="""Profile resolution in meters Default=100m.""", default=100, type=float
    )
    profiles_parser.add_argument(
        "--thickness_url",
        help="""Path to thickness dataset.""",
        default=None,
    )
    profiles_parser.add_argument(
        "--velocity_url",
        help="""Path to velocity dataset.""",
        default=None,
    )
    profiles_parser.add_argument(
        "--velocity_cmap",
        help="""Matplotlib colormap used for overlay. Default: 'speeds-colorblind', a custome colormaps.""",
        type=str,
        default="speed_colorblind",
    )
    profiles_parser.add_argument("INFILES", nargs="*", help="PISM experiment files", default=None)

    options = profiles_parser.parse_args()

    profile_result_dir = Path(options.result_dir)
    profile_result_dir.mkdir(parents=True, exist_ok=True)
    profile_figure_dir = profile_result_dir / Path("figures")
    profile_figure_dir.mkdir(parents=True, exist_ok=True)
    profile_output_dir = profile_result_dir / Path("files")
    profile_output_dir.mkdir(parents=True, exist_ok=True)
    crs = options.crs
    obs_sigma = options.sigma
    obs_scale_alpha = options.alpha
    profile_resolution = options.segmentize
    profiles_path = Path(options.profiles_url)
    velocity_cmap = options.velocity_cmap
    profiles_gp = gp.read_file(profiles_path).rename(columns={"id": "profile_id", "name": "profile_name"})
    geom = profiles_gp.segmentize(profile_resolution)
    profiles_gp = gp.GeoDataFrame(profiles_gp, geometry=geom)
    profiles_gp = profiles_gp[["profile_id", "profile_name", "geometry"]]

    velocity_file = Path(options.velocity_url)
    velocity_ds = xr.open_dataset(velocity_file, chunks="auto")

    its_live_units_dict = {
        "vx": "m/yr",
        "vy": "m/yr",
        "v": "m/yr",
        "vx_err": "m/yr",
        "vy_err": "m/yr",
        "v_err": "m/yr",
        "rock": "1",
        "count": "1",
        "ocean": "1",
        "ice": "1",
    }

    for k, v in its_live_units_dict.items():
        velocity_ds[k].attrs["units"] = v

    print("Opening experiments")
    exp_files = [Path(x) for x in options.INFILES]
    start = time.time()
    exp_ds = xr.open_mfdataset(
        exp_files,
        preprocess=partial(
            preprocess_nc,
            drop_dims=["z", "zb"],
            drop_vars=["timestamp", "shelfbtemp", "effective_ice_surface_temp", "ice_surface_temp", "hardav"],
        ),
        concat_dim="exp_id",
        combine="nested",
        chunks="auto",
        engine="h5netcdf",
        parallel=True,
    )
    time_elapsed = time.time() - start
    print(f"Time elapsed {time_elapsed:.0f}s")

    if options.thickness_url:
        thickness_file = Path(options.thickness_url)
        thickness_ds = xr.open_dataset(thickness_file, chunks="auto").interp_like(velocity_ds)
        velocity_ds = velocity_ds.fluxes.add_fluxes(thickness_ds=thickness_ds)
        exp_ds = exp_ds.fluxes.add_fluxes(
            thickness_var="thk",
            velocity_vars={"x": "uvelsurf", "y": "vvelsurf"},
            flux_vars={"x": "sim_ice_mass_flux_x", "y": "sim_ice_mass_flux_y"},
        )

    stats: List[str] = ["rmsd", "pearson_r"]
    stats_kwargs = {"obs_var": "v_normal", "sim_var": "velsurf_normal"}

    cluster = LocalCluster(n_workers=options.n_jobs, threads_per_worker=1)
    client = Client(cluster)
    n_jobs = len(client.ncores())
    print(f"Open client in browser: {client.dashboard_link}")

    with client:
        start = time.time()
        velocity_ds_scattered = client.scatter(velocity_ds)
        exp_ds_scattered = client.scatter(exp_ds)
        futures = []
        for _, p in profiles_gp.iterrows():
            future = client.submit(
                process_profile,
                p,
                velocity_ds_scattered,
                exp_ds_scattered,
                stats=stats,
                compute_profile_normal=True,
                stats_kwargs=stats_kwargs,
            )
            futures.append(future)
        futures_computed = client.compute(futures)
        progress(futures_computed)
        obs_sims_profiles = client.gather(futures_computed)

        time_elapsed = time.time() - start
        print(f"Time elapsed {time_elapsed:.0f}s")

        n_partitions = 1
        profiles = dask_geopandas.from_geopandas(
            gp.GeoDataFrame(profiles_gp, geometry=profiles_gp.geometry), npartitions=n_partitions
        )

        def concat(profiles_df, profiles_ds):
            """
            Concatenate a merged profiles
            """
            return dd.concat(
                [
                    merge_on_intersection(profiles_df, p.mean(["profile_axis"], skipna=True).to_dask_dataframe())
                    for p in profiles_ds
                ]
            )

        print("Merging dataframes")
        start = time.time()

        profiles_scattered = client.scatter(profiles)
        obs_sims_profiles_scattered = client.scatter(obs_sims_profiles)
        futures = client.submit(concat, profiles_scattered, obs_sims_profiles_scattered)
        progress(futures)
        stats_profiles = client.gather(futures).compute().reset_index(drop=True)

        time_elapsed = time.time() - start
        print(f"Time elapsed {time_elapsed:.0f}s")

    gris_ds = xr.open_dataset(Path("/Users/andy/Google Drive/My Drive/data/MCdataset/BedMachineGreenland-v5.nc"))
    surface_da = gris_ds["surface"]
    overlay_da = velocity_ds["v"].where(velocity_ds["ice"])

    start = time.time()
    print("Plotting glaciers")
    with tqdm_joblib(tqdm(desc="Plotting glaciers", total=len(stats_profiles))) as progress_bar:
        Parallel(n_jobs=n_jobs)(
            delayed(plot_glacier)(
                p,
                surface_da,
                overlay_da,
                profile_figure_dir,
                cmap=velocity_cmap,
            )
            for _, p in stats_profiles.iterrows()
        )
    time_elapsed = time.time() - start
    print(f"Time elapsed {time_elapsed:.0f}s")

    with tqdm_joblib(tqdm(desc="Plotting profiles", total=len(profiles_gp))) as progress_bar:
        Parallel(n_jobs=n_jobs)(
            delayed(plot_profile)(ds, profile_figure_dir, alpha=obs_scale_alpha, sigma=obs_sigma)
            for ds in obs_sims_profiles
        )
