import hashlib
import os
import subprocess
from itertools import groupby
from typing import Any, Dict, Tuple, List

import msgpack
import networkx as nx

from charge.settings import NAUTY_EXC
from charge.util import bfs_nodes

Color = Tuple[bool, str]
"""Nodes are colored by whether they are the core node, and then by atom type."""

Partition = List[Tuple[Color, List[int]]]
"""A Partition is a list of groups of node indexes, grouped and sorted by color."""

Edges = List[Tuple[int, int]]
"""Edges are pairs of node indexes."""

AdjacencyLists = List[Tuple[int, List[int]]]
"""Dreadnaut's way of describing a graph's topology, maps each node to its neighbors."""

class Nauty:
    """Manages a dreadnaut process and communicates with it.

    Args:
        executable: The path to the dreadnaut executable. If not \
                specified, search the path for one.
    """
    def __init__(self, executable: str=NAUTY_EXC) -> None:
        if not os.path.isfile(executable) or not os.access(executable, os.X_OK):
            raise ValueError('Could not find dreadnaut executable at: "%s". Did you install nauty (http://users.cecs.'
                             'anu.edu.au/~bdm/nauty/)?' % executable)
        self.exe = executable
        self.__process = None
        self.__ensure_dreadnaut_running()

    def __del__(self):
        try:
            if self.__process is not None:
                if not self.__process.poll():
                    self.__process.stdin.write('q'.encode('utf-8'))
                    self.__process.stdin.close()
                    self.__process.stdout.close()
                    self.__process.stderr.close()
                self.__process.wait(timeout=1)
        except ValueError:
            pass
        except AttributeError:
            # It seems that it's possible somehow for __process to not exist
            pass
        except TypeError:
            # Looks like we have different threads deleting simultaneously?
            # At any rate it seems that self.__process can be None in the
            # wait() call.
            pass

    def canonize_neighborhood(self, graph: nx.Graph, core: Any, shell: int, color_key='atom_type') -> str:
        """Calculate a canonical key for a neighborhood of an atom.

        Given a molecule graph and an atom in that molecule, this \
        function finds the neighborhood of the given atom of the given \
        depth, and returns a string that uniquely identifies that \
        neighborhood.

        A neighborhood comprises the given atom, any atoms at most \
        shell covalent bonds away from it, and any covalent bonds \
        between those atoms.

        Neighborhoods that consist of atoms with the same colors, \
        connected in the same way, will return the same key.

        Args:
            graph: A molecule's atomic graph.
            core: A node in graph, the core of the neighborhood.
            shell: Shell size to use when creating the neighborhood.
            color_key: Attribute key to use to determine atom color.

        Returns:
            A string unique to the neighborhood.
        """
        if shell > 0:
            fragment = graph.subgraph(bfs_nodes(graph, core, max_depth=shell))
        else:
            fragment = graph.subgraph([core])

        result = self.canonize(fragment, color_key=color_key, core=core)
        return result

    def canonize(self, graph: nx.Graph, color_key='atom_type', core: Any=None) -> str:
        """Calculate a canonical key for a molecular graph.

        Two graphs that consist of atoms with the same colors, \
        connected in the same way, will return the same key.

        If a core is given, it must have the same color in both graphs \
        for them to return an identical key, and the same relative \
        position. Note that the same graph may yield the same key for \
        different core atoms, if they are indistinguishable. For
        example, a methane molecule will give the same key regardless \
        of which of its hydrogen atoms is designated as the core, but \
        will give a different key if the carbon atom is selected in \
        one graph and a hydrogen in the other.

        Args:
            graph: An atomic (sub)graph.
            color_key: Attribute key to use to determine atom color.
            core: A node in graph that is the core of the graph.

        Returns:
            A string unique to the graph.
        """
        node_colors = list()
        for node, color_str in graph.nodes(data=color_key):
            node_colors.append((node == core, color_str))

        nauty_input = self.__make_nauty_input(graph, node_colors)

        self.__ensure_dreadnaut_running()
        nauty_output = self.__communicate(nauty_input)

        canonical_node_ids, adjacency_lists = self.__parse_nauty_output(nauty_output)
        canonical_node_colors = self.__canonical_node_colors(canonical_node_ids, node_colors)
        canonical_edges = self.__canonical_edges(adjacency_lists)
        key = self.__make_hash(canonical_node_colors, canonical_edges)
        return key

    def __ensure_dreadnaut_running(self):
        """Starts dreadnaut if it isn't running."""
        if not self.__process or self.__process.poll():
            self.__process = subprocess.Popen(
                [self.exe],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                close_fds=True
            )

    def __communicate(self, input_str: str) -> str:
        """Sends input to a running dreadnaut, and returns output.

        Args:
            input_str: The input to send to the dreadnaut process.

        Returns:
            The corresponding output produced by dreadnaut.
        """
        self.__process.stdin.write(input_str.encode())
        self.__process.stdin.flush()

        out = self.__process.stdout.read(1000)
        while not b'END' in out:
            out += self.__process.stdout.read(1000)

        return out.strip().decode()


    def __make_nauty_input(
            self,
            graph: nx.Graph,
            node_colors: List[Color]
            ) -> str:
        """Creates a dreadnaut input description of a graph.

        This function creates a string which, when fed to dreadnaut, \
        will cause it to calculate a canonical description of the \
        given graph.

        Args:
            graph: A molecular graph.
            node_colors: The colors of the nodes, in the same order \
                    as the nodes are returned by graph.nodes().

        Returns:
            A string to pass to dreadnaut.
        """
        node_to_index = { v: i for i, v in enumerate(graph.nodes()) }

        nauty_edges = self.__make_nauty_edges(graph.edges(), node_to_index)
        partition = self.__make_partition(list(graph.nodes()), node_colors, node_to_index)

        edges_str = self.__format_edges(nauty_edges)
        partition_str = self.__format_partition(partition)

        input_str = ' n={num_atoms} g {edges}. f=[{partition}] cxb"END\n"->>\n'.format(
                num_atoms=graph.number_of_nodes(),
                edges=edges_str,
                partition=partition_str)

        return input_str

    def __make_nauty_edges(
            self,
            edges: List[Tuple[Any, Any]],
            node_to_index: Dict[Any, int]
            ) -> Edges:
        """Convert a set of edges to use node indexes.

        Args:
            edges: A list of pairs of graph nodes.
            node_to_index: A map from nodes to their indexes.

        Returns:
            The same pairs, but expressed using node indexes.
        """
        nauty_edges = list()
        for u, v in edges:
            nauty_edges.append((node_to_index[u], node_to_index[v]))
        nauty_edges.sort()
        return nauty_edges

    def __make_partition(
            self,
            nodes: List[Any],
            node_colors: List[Color],
            node_to_index: Dict[Any, int]
            ) -> Partition:
        """Organises atoms by color, for passing to dreadnaut.

        Args:
            nodes: A list of graph nodes.
            node_colors: A list of corresponding node colors.
            node_to_index: A map from graph nodes to their indexes.

        Returns:
            A list of groups of node indexes, grouped and sorted by \
                    color.
        """
        def by_color(node_and_color: Tuple[int, Color]) -> Color:
            return node_and_color[1]

        def get_node(node_and_color: Tuple[int, Color]) -> int:
            return node_and_color[0]

        colored_nauty_nodes = list()
        for node_id, color in enumerate(node_colors):
            colored_nauty_nodes.append((node_to_index[nodes[node_id]], color))

        colored_nauty_nodes.sort(key=by_color)

        partition = list()
        for color, node_and_colors in groupby(colored_nauty_nodes, key=by_color):
            nauty_ids = sorted(map(get_node, node_and_colors))
            partition.append((color, nauty_ids))

        return partition

    def __format_edges(self, edges: Edges) -> str:
        """Create a dreadnaut representation of the given edges.

        Args:
            edges: A list of pairs of node indexes.

        Returns:
            A string describing the edges in dreadnaut format.
        """
        nauty_edge_strings = list()
        for u, v in edges:
            nauty_edge_strings.append('{}:{}'.format(u, v))

        return ';'.join(nauty_edge_strings)

    def __format_partition(
            self,
            partition: Partition
            ) -> str:
        """Create a dreadnaut representation of an atom partition.

        Args:
            partition: A list of groups of node indexes, grouped and \
                    sorted by color.

        Returns:
            A string describing the partition in dreadnaut format.
        """
        nauty_cell_strings = list()
        for _, nauty_ids in partition:
            nauty_id_strs = map(str, nauty_ids)
            nauty_cell_strings.append(','.join(nauty_id_strs))

        return '|'.join(nauty_cell_strings)

    def __parse_nauty_output(
            self,
            nauty_output: str
            ) -> Tuple[List[int], AdjacencyLists]:
        """Parses textual nauty output.

        This function reads the dreadnaut output and extracts the \
        canonically ordered node list, and the adjacency lists.

        Args:
            nauty_output: The output produced by dreadnaut.

        Returns:
            A list of node indexes, in canonical order, and a list of \
                    adjacency lists per node, using node indexes.
        """
        def extract_data(nauty_output: str) -> List[str]:
            """Skips header output and returns list of data lines."""
            data = nauty_output.split('seconds')[-1].strip()
            return data.split('\n')

        def extract_canonical_nodes_ids(
                lines: List[str]
                ) -> Tuple[List[int], List[str]]:
            """Returns node indexes in canonical order, and the
            remaining lines.
            """
            canonical_nodes_ids = list()
            i = 0
            while ':' not in lines[i]:
                canonical_ids = map(int, lines[i].split())
                canonical_nodes_ids.extend(canonical_ids)
                i += 1
            return canonical_nodes_ids, lines[i:-1]

        def extract_adjacency_lists(
                lines: List[str]
                ) -> Tuple[int, List[int]]:
            """Extracts pairs of node id, neighbours list from nauty
            output."""
            adjacency_pairs = list()
            for line in lines:
                parts = line.split(':')
                node_id = int(parts[0])
                neighbors = parts[1][0:-1]
                neighbors_ids = list(map(int, neighbors.split()))
                adjacency_pairs.append((node_id, neighbors_ids))
            return adjacency_pairs


        lines = extract_data(nauty_output)
        canonical_nodes_ids, lines = extract_canonical_nodes_ids(lines)
        adjacency_lists = extract_adjacency_lists(lines)

        return canonical_nodes_ids, adjacency_lists

    def __canonical_node_colors(
            self,
            canonical_node_ids: List[int],
            node_colors: List[Color]
            ) -> List[Color]:
        """Returns node colors in canonical order.

        Args:
            canonical_node_ids: Nauty node ids, in canonical order.
            node_colors: List of node colors, indexed by node index.

        Returns:
            A list of node colors in canonical order.
        """
        def get_color(nauty_id: int) -> Color:
            return node_colors[nauty_id]

        return list(map(get_color, canonical_node_ids))


    def __canonical_edges(
            self,
            adjacency_lists: AdjacencyLists
            ) -> Edges:
        """Returns a list of edges, as pairs of node indexes.

        Args:
            adjacency_lists: The adjacency lists output by dreadnaut.

        Returns:
            The corresponding list of edges.
        """
        canonical_edges = list()
        for node_id, neighbors in adjacency_lists:
            for neighbor_id in neighbors:
                canonical_edges.append((node_id, neighbor_id))
        canonical_edges.sort()

        return canonical_edges

    def __make_hash(
            self,
            canonical_nodes: List[Color],
            edges: Edges
            ) -> str:
        """Creates a unique string from dreadnaut output.

        This function creates a fixed-size string from the given \
        arguments. It does not canonicalize anything itself; to get \
        it to produce an identical string, you have to give it \
        identical arguments.

        The one exception to this is a hash collision, but the \
        probability of that is less than 1 in 10^30 or so, so not even \
        the birthday paradox on a sizeable database is enough to make \
        that happen in practice.

        Args:
            canonical_nodes: A list of node colors in canonical order.
            edges: A list of edges, using node indexes.
        """
        canonical_signature = [canonical_nodes, edges]
        canonical_bytes = msgpack.packb(canonical_signature)
        return hashlib.md5(canonical_bytes).hexdigest()
