from __future__ import division, print_function, absolute_import

__copyright__ = "Copyright (C) 2014 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import six
from six.moves import range, zip

from pytools import Record

import numpy as np
import pyopencl as cl
import pyopencl.array  # noqa
import modepy as mp

import logging
logger = logging.getLogger(__name__)


# {{{ boundary connection

class _ConnectionBatchData(Record):
    pass


def _build_boundary_connection(queue, vol_discr, bdry_discr, connection_data):
    from meshmode.discretization.connection import (
            InterpolationBatch, DiscretizationConnectionElementGroup,
            DiscretizationConnection)

    connection_groups = []
    for igrp, (vol_grp, bdry_grp) in enumerate(
            zip(vol_discr.groups, bdry_discr.groups)):
        connection_batches = []
        mgrp = vol_grp.mesh_el_group

        for face_id in range(len(mgrp.face_vertex_indices())):
            data = connection_data[igrp, face_id]

            bdry_unit_nodes_01 = (bdry_grp.unit_nodes + 1)*0.5
            result_unit_nodes = (np.dot(data.A, bdry_unit_nodes_01).T + data.b).T

            connection_batches.append(
                    InterpolationBatch(
                        from_group_index=igrp,
                        from_element_indices=cl.array.to_device(
                            queue,
                            vol_grp.mesh_el_group.element_nr_base
                            + data.group_source_element_indices)
                        .with_queue(None),
                        to_element_indices=cl.array.to_device(
                            queue,
                            bdry_grp.mesh_el_group.element_nr_base
                            + data.group_target_element_indices)
                        .with_queue(None),
                        result_unit_nodes=result_unit_nodes,
                        to_element_face=face_id
                        ))

        connection_groups.append(
                DiscretizationConnectionElementGroup(
                    connection_batches))

    return DiscretizationConnection(
            vol_discr, bdry_discr, connection_groups)

# }}}


# {{{ pull together boundary vertices

def _get_face_vertices(mesh, boundary_tag):
    # a set of volume vertex numbers
    bdry_vertex_vol_nrs = set()

    if boundary_tag is not None:
        # {{{ boundary faces

        btag_bit = mesh.boundary_tag_bit(boundary_tag)

        for fagrp_map in mesh.facial_adjacency_groups:
            bdry_grp = fagrp_map.get(None)
            if bdry_grp is None:
                continue

            assert (bdry_grp.neighbors < 0).all()

            grp = mesh.groups[bdry_grp.igroup]

            nb_el_bits = -bdry_grp.neighbors
            face_relevant_flags = (nb_el_bits & btag_bit) != 0

            for iface, fvi in enumerate(grp.face_vertex_indices()):
                bdry_vertex_vol_nrs.update(
                        grp.vertex_indices
                        [bdry_grp.elements[face_relevant_flags]]
                        [:, np.array(fvi, dtype=np.intp)]
                        .flat)

        return np.array(sorted(bdry_vertex_vol_nrs), dtype=np.intp)

        # }}}
    else:
        # For interior faces, this is likely every vertex in the book.
        # Don't ever bother trying to cut the list down.

        return np.arange(mesh.nvertices, dtype=np.intp)

# }}}


def make_face_restriction(discr, group_factory, boundary_tag):
    """Create a mesh, a discretization and a connection to restrict
    a function on *discr* to its values on the edges of element faces
    denoted by *boundary_tag*.

    :arg boundary_tag: The boundary tag for which to create a face
        restriction. May be *None* to indicate interior faces.

    :return: a tuple ``(bdry_mesh, bdry_discr, connection)``
    """

    logger.info("building face restriction: start")

    # {{{ gather boundary vertices

    bdry_vertex_vol_nrs = _get_face_vertices(discr.mesh, boundary_tag)

    vol_to_bdry_vertices = np.empty(
            discr.mesh.vertices.shape[-1],
            discr.mesh.vertices.dtype)
    vol_to_bdry_vertices.fill(-1)
    vol_to_bdry_vertices[bdry_vertex_vol_nrs] = np.arange(
            len(bdry_vertex_vol_nrs), dtype=np.intp)

    bdry_vertices = discr.mesh.vertices[:, bdry_vertex_vol_nrs]

    # }}}

    from meshmode.mesh import Mesh, SimplexElementGroup
    bdry_mesh_groups = []
    connection_data = {}

    btag_bit = discr.mesh.boundary_tag_bit(boundary_tag)

    for igrp, (grp, fagrp_map) in enumerate(
            zip(discr.groups, discr.mesh.facial_adjacency_groups)):

        mgrp = grp.mesh_el_group

        if not isinstance(mgrp, SimplexElementGroup):
            raise NotImplementedError("can only take boundary of "
                    "SimplexElementGroup-based meshes")

        # {{{ pull together per-group face lists

        group_boundary_faces = []

        if boundary_tag is not None:
            bdry_grp = fagrp_map.get(None)
            if bdry_grp is not None:
                nb_el_bits = -bdry_grp.neighbors
                face_relevant_flags = (nb_el_bits & btag_bit) != 0

                group_boundary_faces.extend(
                            zip(
                                bdry_grp.elements[face_relevant_flags],
                                bdry_grp.element_faces[face_relevant_flags]))

        else:
            for fagrp in six.itervalues(fagrp_map):
                if fagrp.ineighbor_group is None:
                    # boundary faces -> not looking for those
                    continue

                group_boundary_faces.extend(
                        zip(fagrp.elements, fagrp.element_faces))

        # }}}

        # {{{ Preallocate arrays for mesh group

        ngroup_bdry_elements = len(group_boundary_faces)
        vertex_indices = np.empty(
                (ngroup_bdry_elements, mgrp.dim+1-1),
                mgrp.vertex_indices.dtype)

        bdry_unit_nodes = mp.warp_and_blend_nodes(mgrp.dim-1, mgrp.order)
        bdry_unit_nodes_01 = (bdry_unit_nodes + 1)*0.5

        vol_basis = mp.simplex_onb(mgrp.dim, mgrp.order)
        nbdry_unit_nodes = bdry_unit_nodes_01.shape[-1]
        nodes = np.empty(
                (discr.ambient_dim, ngroup_bdry_elements, nbdry_unit_nodes),
                dtype=np.float64)

        # }}}

        grp_face_vertex_indices = mgrp.face_vertex_indices()
        grp_vertex_unit_coordinates = mgrp.vertex_unit_coordinates()

        # batch by face_id

        batch_base = 0

        for face_id in range(len(grp_face_vertex_indices)):
            batch_boundary_el_numbers_in_grp = np.array(
                    [
                        ibface_el
                        for ibface_el, ibface_face in group_boundary_faces
                        if ibface_face == face_id],
                    dtype=np.intp)

            new_el_numbers = np.arange(
                    batch_base,
                    batch_base + len(batch_boundary_el_numbers_in_grp))

            # {{{ no per-element axes in these computations

            # Find boundary vertex indices
            loc_face_vertices = list(grp_face_vertex_indices[face_id])

            # Find unit nodes for boundary element
            face_vertex_unit_coordinates = \
                    grp_vertex_unit_coordinates[loc_face_vertices]

            # Find A, b such that A [e_1 e_2] + b = [r_1 r_2]
            # (Notation assumes that the volume is 3D and the face is 2D.
            # Code does not.)

            b = face_vertex_unit_coordinates[0]
            A = (  # noqa
                    face_vertex_unit_coordinates[1:]
                    - face_vertex_unit_coordinates[0]).T

            face_unit_nodes = (np.dot(A, bdry_unit_nodes_01).T + b).T

            resampling_mat = mp.resampling_matrix(
                    vol_basis,
                    face_unit_nodes, mgrp.unit_nodes)

            # }}}

            # {{{ build information for mesh element group

            # Find vertex_indices
            glob_face_vertices = mgrp.vertex_indices[
                    batch_boundary_el_numbers_in_grp][:, loc_face_vertices]
            vertex_indices[new_el_numbers] = \
                    vol_to_bdry_vertices[glob_face_vertices]

            # Find nodes
            nodes[:, new_el_numbers, :] = np.einsum(
                    "ij,dej->dei",
                    resampling_mat,
                    mgrp.nodes[:, batch_boundary_el_numbers_in_grp, :])

            # }}}

            connection_data[igrp, face_id] = _ConnectionBatchData(
                    group_source_element_indices=batch_boundary_el_numbers_in_grp,
                    group_target_element_indices=new_el_numbers,
                    A=A,
                    b=b,
                    )

            batch_base += len(batch_boundary_el_numbers_in_grp)

        bdry_mesh_group = SimplexElementGroup(
                mgrp.order, vertex_indices, nodes, unit_nodes=bdry_unit_nodes)
        bdry_mesh_groups.append(bdry_mesh_group)

    bdry_mesh = Mesh(bdry_vertices, bdry_mesh_groups)

    from meshmode.discretization import Discretization
    bdry_discr = Discretization(
            discr.cl_context, bdry_mesh, group_factory)

    with cl.CommandQueue(discr.cl_context) as queue:
        connection = _build_boundary_connection(
                queue, discr, bdry_discr, connection_data)

    logger.info("building face restriction: done")

    return bdry_mesh, bdry_discr, connection

# }}}

# vim: foldmethod=marker
