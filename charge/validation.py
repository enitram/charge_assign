import copy
import json
import math
import os
from collections import MutableMapping
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from warnings import warn

import networkx as nx

from charge.babel import convert_from, IOType
from charge.charge_types import Atom
from charge.chargers import make_charger, MeanCharger, MedianCharger, ModeCharger, ILPCharger, DPCharger, CDPCharger, SymmetricILPCharger, SymmetricDPCharger, SymmetricCDPCharger
from charge.nauty import Nauty
from charge.repository import Repository
from charge.util import AssignmentError


def cross_validate_methods(
        data_location: str,
        data_type: IOType = IOType.LGF,
        min_shell: Optional[int] = None,
        max_shell: Optional[int] = None
        ) -> Tuple[Dict[str, Dict[bool, float]], Dict[str, Dict[bool, float]]]:
    """Cross-validates all methods on the given molecule data.

    Args:
        data_location: Path to the directory with the molecule data.
        data_type: Format of the molecule data to expect.
        min_shell: Smallest shell size to use.
        max_shell: Largest shell size to use.

    Returns:
        Dictionaries keyed by charger name and whether IACM atoms were \
                used, containing the mean absolute error and the mean \
                square error respectively.
    """
    repo = Repository.create_from(data_location, data_type, min_shell, max_shell,
            traceable=True)
    shell = range[max_shell, min_shell-1, -1]

    mean_abs_err = dict()
    mean_sq_err = dict()

    for charger_type in [MeanCharger, MedianCharger, ModeCharger, ILPCharger, DPCharger, CDPCharger, SymmetricILPCharger, SymmetricILPCharger, SymmetricCDPCharger]:
        charger_name = charger_type.__name__
        mean_abs_err[charger_name] = dict()
        mean_sq_err[charger_name] = dict()
        for iacm in [True, False]:
            mae, mse = (
                    cross_validate_molecules(
                    charger_name, iacm, data_location, data_type, shell, repo))
            mean_abs_err[charger_name][iacm] = mae
            mean_sq_err[charger_name][iacm] = mse

    return mean_abs_err, mean_sq_err


class AtomReport:
    def __init__(self) -> None:
        self.total_atoms = 0
        """Total number of atoms these statistics are calculated over"""
        self.sum_abs_atom_err = 0.0
        """Mean absolute per-atom charge error"""
        self.sum_sq_atom_err = 0.0
        """Mean squared per-atom charge error"""
        self.atom_errors = list()  # type: List[float]
        """All per-atom charge errors"""

    def mean_abs_atom_err(self):
        if self.total_atoms > 0:
            return self.sum_abs_atom_err / self.total_atoms
        else:
            return 0.0

    def mean_sq_atom_err(self):
        if self.total_atoms > 0:
            return self.sum_sq_atom_err / self.total_atoms
        else:
            return 0.0

    def rms_atom_err(self):
        return math.sqrt(self.mean_sq_atom_err())

    def add_atom_error(self, err: float) -> None:
        self.total_atoms += 1
        self.sum_abs_atom_err += abs(err)
        self.sum_sq_atom_err += err * err
        self.atom_errors.append(err)

    def __iadd__(self, other_report: 'AtomReport') -> 'AtomReport':
        self.total_atoms += other_report.total_atoms
        self.sum_abs_atom_err += other_report.sum_abs_atom_err
        self.sum_sq_atom_err += other_report.sum_sq_atom_err
        self.atom_errors.extend(other_report.atom_errors)
        return self

    def __add__(self, other_report: 'AtomReport') -> 'AtomReport':
        new_report = copy.deepcopy(self)
        new_report += other_report
        return new_report

    def as_dict(self) -> Dict[str, Union[float, int]]:
        return {
                'total_atoms': self.total_atoms,
                'sum_abs_atom_err': self.sum_abs_atom_err,
                'sum_sq_atom_err': self.sum_sq_atom_err,
                'atom_errors': self.atom_errors}

    @staticmethod
    def from_dict(data: Dict[str, Union[float, int]]) -> 'AtomReport':
        new_report = AtomReport()
        new_report.total_atoms = data['total_atoms']
        new_report.sum_abs_atom_err = data['sum_abs_atom_err']
        new_report.sum_sq_atom_err = data['sum_sq_atom_err']
        new_report.atom_errors = data['atom_errors']
        return new_report


class MoleculeReport:
    def __init__(self) -> None:
        self.total_mols = 0
        """Total number of molecules charges were estimated for"""
        self.sum_abs_total_err = 0.0
        """Sum of absolute total charge errors"""
        self.sum_sq_total_err = 0.0
        """Sumo of squared total charge errors"""
        self.total_charge_errors = []  # type: List[Tuple[int, float]]
        """List of total charge errors"""
        self.solver_stats = []  # type: List[int, float, float, int, float]
        """List of solver statistics
        Each tuple contains (molid, mean_abs_atom_err, time, items, scaled_cap)
        """

    # methods for reading results

    def mean_abs_total_err(self):
        if self.total_mols > 0:
            return self.sum_abs_total_err / self.total_mols
        else:
            return 0.0

    def mean_sq_total_err(self):
        if self.total_mols > 0:
            return self.sum_sq_total_err / self.total_mols
        else:
            return 0.0

    def rms_total_err(self):
        return math.sqrt(self.mean_sq_total_err())

    def mean_time(self):
        if self.total_mols > 0:
            return sum([time for _, _, time, _, _ in self.solver_stats]) / self.total_mols
        else:
            return 0.0

    # methods for adding results

    def add_total_charge_error(self, molid: int, err: float) -> None:
        self.total_mols += 1
        self.sum_abs_total_err += abs(err)
        self.sum_sq_total_err += err * err
        self.total_charge_errors.append((molid, err))

    def __iadd__(self, other_report: 'MoleculeReport') -> 'MoleculeReport':
        self.total_mols += other_report.total_mols
        self.sum_abs_total_err += other_report.sum_abs_total_err
        self.sum_sq_total_err += other_report.sum_sq_total_err
        self.total_charge_errors.extend(other_report.total_charge_errors)
        self.solver_stats.extend(other_report.solver_stats)
        return self

    def __add__(self, other_report: 'MoleculeReport') -> 'MoleculeReport':
        new_report = copy.deepcopy(self)
        new_report += other_report
        return new_report

    def as_dict(self) -> Dict[str, Union[float, int]]:
        return {
                'total_mols': self.total_mols,
                'sum_abs_total_err': self.sum_abs_total_err,
                'sub_sq_total_err': self.sum_sq_total_err,
                'total_charge_errors': self.total_charge_errors,
                'solver_stats': self.solver_stats}

    @staticmethod
    def from_dict(data: Dict[str, Union[float, int]]) -> 'MoleculeReport':
        new_report = MoleculeReport()
        new_report.total_mols = data['total_mols']
        new_report.sum_abs_total_err = data['sum_abs_total_err']
        new_report.sum_sq_total_err = data['sub_sq_total_err']
        new_report.total_charge_errors = data['total_charge_errors']
        new_report.solver_stats = data['solver_stats']
        return new_report


class ValidationReport:
    def __init__(self) -> None:
        self.__atom_reports = {
                'C': AtomReport(),
                'H': AtomReport(),
                'N': AtomReport(),
                'O': AtomReport(),
                'P': AtomReport(),
                'S': AtomReport(),
                'Other': AtomReport()
                }
        self.molecule = MoleculeReport()

    def category(self, element: str):
        elem_to_cat = {
                'C': 'C', 'H': 'H', 'N': 'N', 'O': 'O', 'P': 'P', 'S': 'S'}
        category = elem_to_cat.get(element, 'Other')
        return self.__atom_reports[category]

    def __iadd__(self, other_report: 'ValidationReport') -> 'ValidationReport':
        for key in self.__atom_reports:
            self.__atom_reports[key] += other_report.__atom_reports[key]
        self.molecule += other_report.molecule
        return self

    def __add__(self, other_report: 'MoleculeReport') -> 'MoleculeReport':
        new_report = copy.deepcopy(self)
        new_report += other_report
        return new_report

    def as_json(self) -> str:
        return json.dumps({
            'per_atom': {
                'C': self.__atom_reports['C'].as_dict(),
                'H': self.__atom_reports['H'].as_dict(),
                'N': self.__atom_reports['N'].as_dict(),
                'O': self.__atom_reports['O'].as_dict(),
                'P': self.__atom_reports['P'].as_dict(),
                'S': self.__atom_reports['S'].as_dict(),
                'Other': self.__atom_reports['Other'].as_dict()},
            'per_molecule': self.molecule.as_dict()})

    @staticmethod
    def from_json(path: str) -> 'ValidationReport':
        with open(path, 'r') as f:
            data = json.load(f)
        new_report = ValidationReport()
        for category in ['C', 'H', 'N', 'O', 'P', 'S', 'Other']:
            new_report.__atom_reports[category] = AtomReport.from_dict(
                    data['per_atom'][category])
        new_report.molecule = MoleculeReport.from_dict(
                data['per_molecule'])
        return new_report


def cross_validate_molecules(
        charger_type: str,
        iacm: bool,
        data_location: str,
        data_type: IOType = IOType.LGF,
        shell: Union[None, int, Iterable[int]] = None,
        repo: Optional[Repository] = None,
        bucket: int = 0,
        num_buckets: int = 1
        ) -> ValidationReport:
    """Cross-validates a particular method on the given molecule data.

    Runs through all the molecules in the repository, and for each, \
    predicts charges using the given charger type and from the rest of \
    the molecules in the repository.

    If iacm is False, the test molecule is stripped of its charge data \
    and its IACM atom types, leaving only plain elements. It is then \
    matched first against the IACM side of the repository, and if that \
    yields no charges, against the plain element side of the repository.

    If iacm is True, the test molecule is stripped of charges but keeps \
    its IACM atom types. It is then matched against the IACM side of the \
    repository, and if no matches are found, its plain elements are \
    matched against the plain element side.

    If bucket and num_buckets are specified, then this will only run \
    the cross-validation if (molid % num_buckets) == bucket.

    Args:
        charger_type: Name of a Charger-derived class implementing an \
                assignment method.
        iacm: Whether to use IACM or plain element atoms.
        data_location: Path to the directory with the molecule data.
        data_type: Format of the molecule data to expect.
        shell: (List of) shell size(s) to use.
        repo: A Repository with traceable charges.
        bucket: Cross-validate for this bucket.
        num_buckets: Total number of buckets that will run.

    Returns:
        A dict containing AtomReports per element category, and a
        MoleculeReport. Keyed by category name, and 'Molecule' for
        the per-molecule statistics.
    """
    if shell is None:
        min_shell, max_shell = None, None
        wanted_shells = None
    else:
        if isinstance(shell, int):
            shell = [shell]
        min_shell, max_shell = min(shell), max(shell)
        wanted_shells = sorted(shell, reverse=True)

    if repo is None:
        if min_shell is not None:
            repo = Repository.create_from(data_location, data_type, min_shell,
                    max_shell, traceable=True)
        else:
            repo = Repository.create_from(data_location, data_type,
                    traceable=True)

    if wanted_shells is None:
        shells = sorted(repo.charges_iacm.keys(), reverse=True)
    else:
        shells = []
        for s in wanted_shells:
            if s not in repo.charges_iacm.keys():
                msg = 'Shell {} will not be used, as it is not in the repository'
                warn(msg.format(s))
            else:
                shells.append(s)

    nauty = Nauty()

    extension = data_type.get_extension()
    molids = [int(fn.replace(extension, ''))
              for fn in os.listdir(data_location)
              if fn.endswith(extension)]

    report = ValidationReport()

    for molid in molids:
        if (molid % num_buckets) == bucket:
            #print('molid: {}'.format(molid))

            mol_path = os.path.join(data_location, '{}{}'.format(molid, extension))
            with open(mol_path, 'r') as f:
                graph = convert_from(f.read(), data_type)

            report += cross_validate_molecule(repo, molid, graph, charger_type, shells, iacm, nauty)

    return report


def cross_validate_molecule(
        repository: Repository, molid: int, graph: nx.Graph, charger_type: str,
        shells: List[int], iacm: bool, nauty: Nauty
        ) -> ValidationReport:
    """Test prediction for a single molecule.

    Args:
        repository: Repository the molecule is in
        molid: Molid of the molecule to test prediction for
        graph: Molecular graph for the molecule
        charger_type: Name of the Charger class to use
        shells: List of shells to use when predicting
        iacm: Whether to use IACM atom types or not
        nauty: Nauty instance to use for canonization

    Returns:
        A dict of ValidationReports, keyed by element category, with
        the comparison results
    """
    report = ValidationReport()

    filtered_repository = _FilteredRepository(repository, molid)
    charger = make_charger(charger_type, filtered_repository, 3, 10, nauty)

    test_graph = strip_molecule(graph, iacm)
    total_charge = round(sum([charge for _, charge in graph.nodes(data='partial_charge')]))

    try:
        charger.charge(test_graph, total_charge, False, iacm, shells)
    except AssignmentError as e:
        msg = 'Error while predicting charges for molid {} using shells {}: {}'
        warn(msg.format(molid, shells, e))
        return report

    atoms_in_this_mol_report = AtomReport()
    for atom, data in graph.nodes(data=True):
        ref_charge = data['partial_charge']
        element = data['atom_type']
        atom_error = test_graph.node[atom]['partial_charge'] - ref_charge
        report.category(element).add_atom_error(atom_error)
        atoms_in_this_mol_report.add_atom_error(atom_error)

    report.molecule.add_total_charge_error(
            molid, test_graph.graph['total_charge'] - total_charge)

    report.molecule.solver_stats.append((
            molid,
            atoms_in_this_mol_report.mean_abs_atom_err(),
            test_graph.graph['time'],
            test_graph.graph['items'],
            test_graph.graph['scaled_capacity']))

    return report


def strip_molecule(graph: nx.Graph, iacm: bool) -> nx.Graph:
    """Return a copy of the graph with charges removed.

    If iacm is True, also removes IACM atom types.
    """
    stripped_graph = graph.copy()
    for _, data in stripped_graph.nodes(data=True):
        del(data['partial_charge'])
        if not iacm:
            del(data['iacm'])
    return stripped_graph


class _FilteredCharges(MutableMapping):
    """A facade for a charge dict that filters out a given molid.

    Objects of this class wrap a dict of key -> (charge, molid, atom) \
    and if a list of charges is looked up, filter out any molids in \
    their filter list, then return only the charges.
    """
    def __init__(
            self,
            charges: Dict[str, List[Tuple[float, int, Atom]]],
            blocked_molids: List[int]) -> None:
        """Create a _FilteredCharges.

        Args:
            charges: A set of traceable charges.
            blocked_molids: A list of molids to filter out.
        """
        self.__charges = charges
        self.__blocked_molids = blocked_molids

    def __getitem__(self, key: str) -> List[Atom]:
        if key in self.__charges:
            charges = self.__filtered_copy(self.__charges[key])
            if len(charges) == 0:
                raise KeyError
        else:
            raise KeyError()
        return charges

    def __setitem__(self, key: str, value: Any) -> None:
        pass

    def __delitem__(self, key: str) -> None:
        pass

    def __iter__(self):
        pass

    def __len__(self):
        return len(self.__charges)

    def __filtered_copy(self, traceable_charges: List[Tuple[float, int, Atom]]) -> List[float]:
        return [
                charge for charge, molid, _ in traceable_charges
                if molid not in self.__blocked_molids]


class _FilteredRepository:
    """A facade for a Repository that filters out some charges.

    This class partially mimics a Repository, with its charges_iacm and \
    charges_elem attributes. The charges it presents come from an \
    underlying Repository object, but have been filtered to exclude \
    all charges contributed by all molecules isomorphic to a given \
    molecule.

    Attributes:
        charges_iacm: Like the Repository class.
        charges_elem: Like the Repository class.
    """
    def __init__(self, repo: Repository, molid: int) -> None:
        """Create a _FilteredRepository.

        This will also exclude other molecules isomorphic to molid.

        Args:
            molid: Molid of molecule to exclude charges for.
        """
        if molid in repo.iso_iacm:
            iacm_excluded = repo.iso_iacm[molid]
        else:
            iacm_excluded = [molid]

        self.charges_iacm = dict()
        for shell in repo.charges_iacm.keys():
            self.charges_iacm[shell] = _FilteredCharges(
                    repo.charges_iacm[shell], iacm_excluded)

        if molid in repo.iso_elem:
            elem_excluded = repo.iso_elem[molid]
        else:
            elem_excluded = [molid]

        self.charges_elem = dict()
        for shell in repo.charges_elem.keys():
            self.charges_elem[shell] = _FilteredCharges(
                    repo.charges_elem[shell], elem_excluded)
