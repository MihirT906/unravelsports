import logging
import sys
from copy import deepcopy

import pandas as pd

import warnings

from dataclasses import dataclass, field, asdict

from typing import List, Union, Dict, Literal, Any

from kloppy.domain import (
    TrackingDataset,
    Frame,
    Orientation,
    DatasetTransformer,
    DatasetFlag,
    SecondSpectrumCoordinateSystem,
    MetricPitchDimensions,
)

from spektral.data import Graph

from .exceptions import (
    MissingLabelsError,
    MissingDatasetError,
    IncorrectDatasetTypeError,
    KeyMismatchError,
)

from .graph_settings_pl import GraphSettingsPolars
from .dataset import KloppyPolarsDataset
from .features import (
    compute_node_features_pl,
    compute_adjacency_matrix_pl,
    compute_edge_features_pl,
)

from ...utils import *

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
stdout_handler = logging.StreamHandler(sys.stdout)
logger.addHandler(stdout_handler)


@dataclass(repr=True)
class SoccerGraphConverterPolars(DefaultGraphConverter):
    """
    Converts our dataset TrackingDataset into an internal structure

    Attributes:
        dataset (TrackingDataset): Kloppy TrackingDataset.
        labels (dict): Dict with a key per frame_id, like so {frame_id: True/False/1/0}
        graph_id (str, int): Set a single id for the whole Kloppy dataset.
        graph_ids (dict): Frame level control over graph ids.

        The graph_ids will be used to assign each graph an identifier. This identifier allows us to split the CustomSpektralDataset such that
            all graphs with the same id are either all in the test, train or validation set to avoid leakage. It is recommended to either set graph_id (int, str) as
            a match_id, or pass a dictionary into 'graph_ids' with exactly the same keys as 'labels' for more granualar control over the graph ids.
        The latter can be useful when splitting graphs by possession or sequence id. In this case the dict would be {frame_id: sequence_id/possession_id}.
        Note that sequence_id/possession_id should probably be unique for the whole dataset. Perhaps like so {frame_id: 'match_id-sequence_id'}. Defaults to None.

        ball_carrier_threshold (float): The distance threshold to determine the ball carrier. Defaults to 25.0.
        non_potential_receiver_node_value (float): Value between 0 and 1 to assign to the defing team players
    """

    dataset: KloppyPolarsDataset = None

    chunk_size: int = 2_0000
    non_potential_receiver_node_value: float = 0.1

    def __post_init__(self):
        self.pitch_dimensions: MetricPitchDimensions = self.dataset.pitch_dimensions
        self.label_col = self.dataset._label_column
        self.graph_id_col = self.dataset._graph_id_column
        
        self.ball_carrier_threshold = self.dataset.ball_carrier_threshold
        self.dataset = self.dataset.data

        self._sport_specific_checks()
        self.settings = self._apply_settings()
        self.dataset = self._apply_filters()
        
        if self.pad:
            self.dataset = self._apply_padding(df=self.dataset)
    
    @staticmethod   
    def _apply_padding(df: pl.DataFrame) -> pl.DataFrame:
        keep_columns = [
            'timestamp',
            'ball_state',
            'position_name',
            'label',
            'graph_id'
        ]
        empty_columns = [
            'id', 'x', 'y', 'z', 'vx', 'vy',
            'vz', 'v', 'ax', 'ay', 'az', 'a'
        ]
        group_by_columns = ['game_id', 'period_id', 'frame_id', 'team_id', 'ball_owning_team_id']
        
        counts = (
            df.group_by(group_by_columns)
            .agg(
                pl.len().alias('count'),
                *[pl.first(col).alias(col) for col in keep_columns]
            )
        )
        
        counts = counts.with_columns([
            pl.when(pl.col('team_id') == "ball")
            .then(1)
            .when(pl.col('team_id') == pl.col('ball_owning_team_id'))
            .then(11)
            .otherwise(11)
            .alias('target_length')
        ])
        
        groups_to_pad = (
            counts
            .filter(pl.col('count') < pl.col('target_length'))
            .with_columns(
                (pl.col('target_length') - pl.col('count')).alias('repeats')
            )
        )
        
        if len(groups_to_pad) == 0:
            return df
            
        padding_rows = []
        for row in groups_to_pad.iter_rows(named=True):
            base_row = {col: row[col] for col in keep_columns + group_by_columns}
            padding_rows.extend([base_row] * row['repeats'])
        
        padding_df = pl.DataFrame(padding_rows)
        
        schema = df.schema
        padding_df = padding_df.with_columns([
            pl.lit(0.0 if schema[col] != pl.String else "None").cast(schema[col]).alias(col)
            for col in empty_columns
        ])
        
        padding_df = padding_df.select(df.columns)
        
        result = pl.concat([df, padding_df], how='vertical')
        
        total_frames = (
            result.select(['game_id', 'period_id', 'frame_id'])
            .unique()
            .height
        )
        
        frame_completeness = (
            result.group_by(['game_id', 'period_id', 'frame_id'])
            .agg([
                (pl.col('team_id').eq("ball").sum() == 1).alias('has_ball'),
                (pl.col('team_id').eq(pl.col('ball_owning_team_id')).sum() == 11).alias('has_owning_team'),
                ((~pl.col('team_id').eq("ball") & ~pl.col('team_id').eq(pl.col('ball_owning_team_id'))).sum() == 11).alias('has_other_team')
            ])
            .filter(
                pl.col('has_ball') & pl.col('has_owning_team') & pl.col('has_other_team')
            )
        )
        
        complete_frames = frame_completeness.height
        
        dropped_frames = total_frames - complete_frames
        if dropped_frames > 0:
            import warnings
            warnings.warn(
                f"""Setting pad=True drops frames that do not have at least 1 object for the attacking team, defending team or ball.
                This operation dropped {dropped_frames} incomplete frames out of {total_frames} total frames ({(dropped_frames/total_frames)*100:.2f}%)
                """
            )
        
        return result.join(
            frame_completeness,
            on=['game_id', 'period_id', 'frame_id'],
            how='inner'
        )

    def _apply_filters(self):
        return self.dataset.with_columns(
            pl.when(
                (pl.col(self.settings._identifier_column) == self.settings.ball_id)
                & (pl.col("v") > self.settings.max_ball_speed)
            )
            .then(self.settings.max_ball_speed)
            .when(
                (pl.col(self.settings._identifier_column) != self.settings.ball_id)
                & (pl.col("v") > self.settings.max_player_speed)
            )
            .then(self.settings.max_player_speed)
            .otherwise(pl.col("v"))
            .alias("v")
        ).with_columns(
            pl.when(
                (pl.col(self.settings._identifier_column) == self.settings.ball_id)
                & (pl.col("a") > self.settings.max_ball_acceleration)
            )
            .then(self.settings.max_ball_acceleration)
            .when(
                (pl.col(self.settings._identifier_column) != self.settings.ball_id)
                & (pl.col("a") > self.settings.max_player_acceleration)
            )
            .then(self.settings.max_player_acceleration)
            .otherwise(pl.col("a"))
            .alias("a")
        )

    def _apply_settings(self):
        return GraphSettingsPolars(
            pitch_dimensions=self.pitch_dimensions,
            ball_carrier_treshold=self.ball_carrier_threshold,
            max_player_speed=self.max_player_speed,
            max_ball_speed=self.max_ball_speed,
            max_player_acceleration=self.max_player_acceleration,
            max_ball_acceleration=self.max_ball_acceleration,
            self_loop_ball=self.self_loop_ball,
            adjacency_matrix_connect_type=self.adjacency_matrix_connect_type,
            adjacency_matrix_type=self.adjacency_matrix_type,
            label_type=self.label_type,
            defending_team_node_value=self.defending_team_node_value,
            non_potential_receiver_node_value=self.non_potential_receiver_node_value,
            random_seed=self.random_seed,
            pad=self.pad,
            verbose=self.verbose,
        )

    def _sport_specific_checks(self):
        if not isinstance(self.label_col, str):
            raise Exception("'label_col' should be of type string (str)")

        if not isinstance(self.graph_id_col, str):
            raise Exception("'graph_id_col' should be of type string (str)")

        if not isinstance(self.chunk_size, int):
            raise Exception("chunk_size should be of type integer (int)")

        if not self.label_col in self.dataset.columns and not self.prediction:
            raise Exception(
                "Please specify a 'label_col' and add that column to your 'dataset' or set 'prediction=True' if you want to use the converted dataset to make predictions on."
            )

        if not self.graph_id_col in self.dataset.columns:
            raise Exception(
                "Please specify a 'graph_id_col' and add that column to your 'dataset' ..."
            )

        if self.ball_carrier_threshold and not isinstance(
            self.ball_carrier_threshold, float
        ):
            raise Exception("'ball_carrier_threshold' should be of type float")

        if self.non_potential_receiver_node_value and not isinstance(
            self.non_potential_receiver_node_value, float
        ):
            raise Exception(
                "'non_potential_receiver_node_value' should be of type float"
            )
            
    @property
    def __exprs_variables(self):
        return [
            "x", "y", "z",
            "v", "vx", "vy", "vz",
            "a", "ax", "ay", "az",
            "team_id", "position_name", "ball_owning_team_id",
            self.graph_id_col,
            self.label_col,
        ]
    
    def __compute(self, args: List[pl.Series]) -> dict:
        d = {col: args[i].to_numpy() for i, col in enumerate(self.__exprs_variables)}
        
        if not np.all(d[self.graph_id_col] == d[self.graph_id_col][0]):
            raise Exception(
                "GraphId selection contains multiple different values. Make sure each graph_id is unique by at least game_id and frame_id..."
            )

        if not self.prediction and not np.all(d[self.label_col] == d[self.label_col][0]):
            raise Exception(
                """Label selection contains multiple different values for a single selection (group by) of game_id and frame_id, 
                make sure this is not the case. Each group can only have 1 label."""
            )
        
        ball_carrier_idx = get_ball_carrier_idx(
            x=d['x'], y=d['y'], z=d['z'],
            team=d['team_id'],
            possession_team=d['ball_owning_team_id'],
            ball_id=self.settings.ball_id,
            threshold=self.settings.ball_carrier_treshold,
        )
        adjacency_matrix = compute_adjacency_matrix_pl(
            team=d['team_id'],
            possession_team=d['ball_owning_team_id'],
            settings=self.settings,
            ball_carrier_idx=ball_carrier_idx,
        )
        edge_features = compute_edge_features_pl(
            adjacency_matrix=adjacency_matrix,
            p3d=np.stack((d['x'], d['y'], d['z']), axis=-1),
            p2d=np.stack((d['x'], d['y']), axis=-1),
            s=d['v'],
            velocity=np.stack((d['vx'], d['vy']), axis=-1),
            team=d['team_id'],
            settings=self.settings,
        )
        node_features = compute_node_features_pl(
            d['x'],
            d['y'],
            s=d['v'],
            velocity=np.stack((d['vx'], d['vy']), axis=-1),
            team=d['team_id'],
            possession_team=d['ball_owning_team_id'],
            is_gk=(d['position_name'] == self.settings.goalkeeper_id).astype(int),
            settings=self.settings,
        )
        return {
            "e": pl.Series(
                [edge_features.tolist()], dtype=pl.List(pl.List(pl.Float64))
            ),
            "x": pl.Series(
                [node_features.tolist()], dtype=pl.List(pl.List(pl.Float64))
            ),
            "a": pl.Series(
                [adjacency_matrix.tolist()], dtype=pl.List(pl.List(pl.Int32))
            ),
            "e_shape_0": edge_features.shape[0],
            "e_shape_1": edge_features.shape[1],
            "x_shape_0": node_features.shape[0],
            "x_shape_1": node_features.shape[1],
            "a_shape_0": adjacency_matrix.shape[0],
            "a_shape_1": adjacency_matrix.shape[1],
            self.graph_id_col: d[self.graph_id_col][0],
            self.label_col: d[self.label_col][0],
        }
    
    def _convert(self):
        result_df = self.dataset.group_by(
            ["game_id", "frame_id"], maintain_order=True
        ).agg(
            pl.map_groups(
                exprs=self.__exprs_variables,
                function=self.__compute,
            ).alias("result_dict")
        )

        graph_df = result_df.with_columns(
            [
                pl.col("result_dict").struct.field("a").alias("a"),
                pl.col("result_dict").struct.field("e").alias("e"),
                pl.col("result_dict").struct.field("x").alias("x"),
                pl.col("result_dict").struct.field("e_shape_0").alias("e_shape_0"),
                pl.col("result_dict").struct.field("e_shape_1").alias("e_shape_1"),
                pl.col("result_dict").struct.field("x_shape_0").alias("x_shape_0"),
                pl.col("result_dict").struct.field("x_shape_1").alias("x_shape_1"),
                pl.col("result_dict").struct.field("a_shape_0").alias("a_shape_0"),
                pl.col("result_dict").struct.field("a_shape_1").alias("a_shape_1"),
                pl.col("result_dict")
                .struct.field(self.graph_id_col)
                .alias(self.graph_id_col),
                pl.col("result_dict")
                .struct.field(self.label_col)
                .alias(self.label_col),
            ]
        )

        return graph_df.drop("result_dict")
    
    

    def to_graph_frames(self) -> List[dict]:
        def __convert_to_graph_data_list(df):
            lazy_df = df.lazy()

            graph_list = []

            for chunk in lazy_df.collect().iter_slices(self.chunk_size):
                chunk_graph_list = [
                    {
                        "a": make_sparse(
                            flatten_to_reshaped_array(
                                arr=chunk["a"][i],
                                s0=chunk["a_shape_0"][i],
                                s1=chunk["a_shape_1"][i],
                            )
                        ),
                        "x": flatten_to_reshaped_array(
                            arr=chunk["x"][i],
                            s0=chunk["x_shape_0"][i],
                            s1=chunk["x_shape_1"][i],
                        ),
                        "e": flatten_to_reshaped_array(
                            arr=chunk["e"][i],
                            s0=chunk["e_shape_0"][i],
                            s1=chunk["e_shape_1"][i],
                        ),
                        "y": np.asarray([chunk[self.label_col][i]]),
                        "id": chunk[self.graph_id_col][i],
                    }
                    for i in range(len(chunk["a"]))
                ]
                graph_list.extend(chunk_graph_list)

            return graph_list
        
        graph_df = self._convert()
        self.graph_frames = self.__convert_to_graph_data_list(graph_df)
        
        return self.graph_frames

    def to_spektral_graphs(self) -> List[Graph]:
        if not self.graph_frames:
            self.to_graph_frames()

        return [
            Graph(
                x=d["x"],
                a=d["a"],
                e=d["e"],
                y=d["y"],
                id=d["id"],
            )
            for d in self.graph_frames
        ]

    def to_pickle(self, file_path: str) -> None:
        """
        We store the 'dict' version of the Graphs to pickle each graph is now a dict with keys x, a, e, and y
        To use for training with Spektral feed the loaded pickle data to CustomDataset(data=pickled_data)
        """
        if not file_path.endswith("pickle.gz"):
            raise ValueError(
                "Only compressed pickle files of type 'some_file_name.pickle.gz' are supported..."
            )

        if not self.graph_frames:
            self.to_graph_frames()

        import pickle
        import gzip
        from pathlib import Path

        path = Path(file_path)

        directories = path.parent
        directories.mkdir(parents=True, exist_ok=True)

        with gzip.open(file_path, "wb") as file:
            pickle.dump(self.graph_frames, file)
