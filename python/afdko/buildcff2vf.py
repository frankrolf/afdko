# Copyright 2017 Adobe. All rights reserved.

from __future__ import print_function, division, absolute_import

import argparse
from copy import deepcopy
import logging
import os
from pkg_resources import parse_version
import sys

from fontTools import varLib, version as fontToolsVersion
from fontTools.cffLib.specializer import commandsToProgram
from fontTools.designspaceLib import DesignSpaceDocument
from fontTools.misc.fixedTools import otRound
from fontTools.misc.psCharStrings import T2OutlineExtractor, T2CharString
from fontTools.varLib.cff import CFF2CharStringMergePen

__version__ = '1.15.0'


# set up for printing progress notes
def progress(self, message, *args, **kws):
    # Note: message must contain the format specifiers for any strings in args.
    level = self.getEffectiveLevel()
    self._log(level, message, args, **kws)


PROGRESS_LEVEL = logging.INFO+5
PROGESS_NAME = "progress"
logging.addLevelName(PROGRESS_LEVEL, PROGESS_NAME)
logger = logging.getLogger(__name__)
logging.Logger.progress = progress


def _validate_path(path_str):
    # used for paths passed to get_options.
    valid_path = os.path.abspath(os.path.realpath(path_str))
    if not os.path.exists(valid_path):
        raise argparse.ArgumentTypeError(
            "'{}' is not a valid path.".format(path_str))
    return valid_path


def get_options(args):
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description=__doc__
    )
    parser.add_argument(
        '--version',
        action='version',
        version=__version__
    )
    parser.add_argument(
        '-v',
        '--verbose',
        action='count',
        default=0,
        help='verbose mode\n'
             'Use -vv for debug mode'
    )
    parser.add_argument(
        '-d',
        '--designspace',
        metavar='PATH',
        dest='design_space_path',
        type=_validate_path,
        help='path to design space file\n',
        required=True
    )
    parser.add_argument(
        '-o',
        '--out',
        metavar='PATH',
        dest='var_font_path',
        help='path to output variable font file. Default is base name\n'
        'of the design space file.\n',
        default=None,
    )
    parser.add_argument(
        '-k',
        '--keep_glyph_names',
        dest='keep_glyph_names',
        action='store_true',
        help='Preserve glyph names in output var font, with a post table\n'
        'format 2.\n',
        default=False,
    )
    parser.add_argument(
        '-c',
        '--compat',
        dest='check_compatibility',
        action='store_true',
        help='Check outline compatibility in source fonts, and fix flat\n'
        'curves.\n',
        default=False,
    )
    options = parser.parse_args(args)
    if not options.var_font_path:
        var_font_path = os.path.splitext(options.design_space_path)[0] + '.otf'
        options.var_font_path = var_font_path

    if not options.verbose:
        level = PROGRESS_LEVEL
        logging.basicConfig(level=level, format="%(message)s")
    else:
        if options.verbose:
            level = logging.INFO
        logging.basicConfig(level=level)
    logger.setLevel(level)

    return options


class MergeTypeError(Exception):
    pass


class CompatibilityPen(CFF2CharStringMergePen):
    def __init__(self, default_commands,
                 glyphName, num_masters, master_idx, roundTolerance=0.5):
        super(CompatibilityPen, self).__init__(
                      default_commands, glyphName, num_masters, master_idx,
                      roundTolerance=0.5)
        self.fixed = False

    def add_point(self, point_type, pt_coords):
        if self.m_index == 0:
            self._commands.append([point_type, [pt_coords]])
        else:
            cmd = self._commands[self.pt_index]
            if cmd[0] != point_type:
                # Fix some issues that show up in some
                # CFF workflows, even when fonts are
                # topologically merge compatible.
                success, new_pt_coords = self.check_and_fix_flat_curve(
                    cmd, point_type, pt_coords)
                if success:
                    logger.progress("Converted between line and curve in "
                                    "source font index '%s' glyph '%s', "
                                    "point index '%s'at '%s'. "
                                    "Please check correction." % (
                                        self.m_index, self.glyphName,
                                        self.pt_index, pt_coords))
                    pt_coords = new_pt_coords
                else:
                    success = self.check_and_fix_closepath(
                            cmd, point_type, pt_coords)
                    if success:
                        # We may have incremented self.pt_index
                        cmd = self._commands[self.pt_index]
                        if cmd[0] != point_type:
                            success = False
                if not success:
                    raise MergeTypeError(
                        point_type, self.pt_index, self.m_index, cmd[0],
                        self.glyphName)
                self.fixed = True
            cmd[1].append(pt_coords)
        self.pt_index += 1

    def make_flat_curve(self, prev_coords, cur_coords):
        # Convert line coords to curve coords.
        dx = self.roundNumber((cur_coords[0] - prev_coords[0])/3.0)
        dy = self.roundNumber((cur_coords[1] - prev_coords[1])/3.0)
        new_coords = [prev_coords[0] + dx,
                      prev_coords[1] + dy,
                      prev_coords[0] + 2*dx,
                      prev_coords[1] + 2*dy
                      ] + cur_coords
        return new_coords

    def make_curve_coords(self, coords, is_default):
        # Convert line coords to curve coords.
        prev_cmd = self._commands[self.pt_index-1]
        if is_default:
            new_coords = []
            for i, cur_coords in enumerate(coords):
                prev_coords = prev_cmd[1][i]
                master_coords = self.make_flat_curve(prev_coords[:2],
                                                     cur_coords)
                new_coords.append(master_coords)
        else:
            cur_coords = coords
            prev_coords = prev_cmd[1][-1]
            new_coords = self.make_flat_curve(prev_coords[:2], cur_coords)
        return new_coords

    def check_and_fix_flat_curve(self, cmd, point_type, pt_coords):
        if (point_type == 'rlineto') and (cmd[0] == 'rrcurveto'):
            is_default = False  # the line is in the master font we are adding
            pt_coords = self.make_curve_coords(pt_coords, is_default)
            success = True
        elif (point_type == 'rrcurveto') and (cmd[0] == 'rlineto'):
            is_default = True  # the line is in the default font commands
            expanded_coords = self.make_curve_coords(cmd[1], is_default)
            cmd[1] = expanded_coords
            cmd[0] = point_type
            success = True
        else:
            success = False
        return success, pt_coords

    def check_and_fix_closepath(self, cmd, point_type, pt_coords):
        """ Some workflows drop a lineto which closes a path.
        Also, if the last segment is a curve in one master,
        and a flat curve in another, the flat curve can get
        converted to a closing lineto, and then dropped.
        Test if:
        1) one master op is a moveto,
        2) the previous op for this master does not close the path
        3) in the other master the current op is not a moveto
        4) the current op in the otehr master closes the current path

        If the default font is missing the closing lineto, insert it,
        then proceed with merging the current op and pt_coords.

        If the current region is missing the closing lineto
        and therefore the current op is a moveto,
        then add closing coordinates to self._commands,
        and increment self.pt_index.

        Note that if this may insert a point in the default font list,
        so after using it, 'cmd' needs to be reset.

        return True if we can fix this issue.
        """
        if point_type == 'rmoveto':
            # If this is the case, we know that cmd[0] != 'rmoveto'

            # The previous op must not close the path for this region font.
            prev_moveto_coords = self._commands[self.prev_move_idx][1][-1]
            prv_coords = self._commands[self.pt_index-1][1][-1]
            if prev_moveto_coords == prv_coords[-2:]:
                return False

            # The current op must close the path for the default font.
            prev_moveto_coords2 = self._commands[self.prev_move_idx][1][0]
            prv_coords = self._commands[self.pt_index][1][0]
            if prev_moveto_coords2 != prv_coords[-2:]:
                return False

            # Add the closing line coords for this region
            # so self._commands, then increment self.pt_index
            # so that the current region op will get merged
            # with the next default font moveto.
            if cmd[0] == 'rrcurveto':
                new_coords = self.make_curve_coords(prev_moveto_coords, False)
            cmd[1].append(new_coords)
            self.pt_index += 1
            return True

        if cmd[0] == 'rmoveto':
            # The previous op must not close the path for the default font.
            prev_moveto_coords = self._commands[self.prev_move_idx][1][0]
            prv_coords = self._commands[self.pt_index-1][1][0]
            if prev_moveto_coords == prv_coords[-2:]:
                return False

            # The current op must close the path for this region font.
            prev_moveto_coords2 = self._commands[self.prev_move_idx][1][-1]
            if prev_moveto_coords2 != pt_coords[-2:]:
                return False

            # Insert the close path segment in the default font.
            # We omit the last coords from the previous moveto
            # is it will be supplied by the current region point.
            # after this function returns.
            new_cmd = [point_type, None]
            prev_move_coords = self._commands[self.prev_move_idx][1][:-1]
            # Note that we omit the last region's coord from prev_move_coords,
            # as that is from the current region, and we will add the
            # current pts' coords from the current region in its place.
            if point_type == 'rlineto':
                new_cmd[1] = prev_move_coords
            else:
                # We omit the last set of coords from the
                # previous moveto, as it will be supplied by the coords
                # for the current region pt.
                new_cmd[1] = self.make_curve_coords(prev_move_coords, True)
            self._commands.insert(self.pt_index, new_cmd)
            return True
        return False

    def getCharStrings(self, num_masters, private=None, globalSubrs=None):
        """ A command look s like:
        [op_name, [
            [source 0 arglist for op],
            [source 1 arglist for op],
            ...
            [source n arglist for op],
        I am not optimising this there, as that will be done when
        the CFF2 Charstring is creating in fontTools.varLib.build().

        If I did, I woudl have to rearragne the arguments to:
        [
        [arg 0 for source 0 ... arg 0 for source n]
        [arg 1 for source 0 ... arg 1 for source n]
        ...
        [arg M for source 0 ... arg M for source n]
        ]
        before calling specialize.
        """
        t2List = []
        merged_commands = self._commands
        for i in range(num_masters):
            commands = []
            for op in merged_commands:
                source_op = [op[0], op[1][i]]
                commands.append(source_op)
            program = commandsToProgram(commands)
            if self._width is not None:
                assert not self._CFF2, (
                    "CFF2 does not allow encoding glyph width in CharString.")
                program.insert(0, otRound(self._width))
            if not self._CFF2:
                program.append('endchar')
            charString = T2CharString(
                program=program, private=private, globalSubrs=globalSubrs)
            t2List.append(charString)
        return t2List


def _get_cs(glyphOrder, charstrings, glyphName):
    if glyphName not in charstrings:
        return None
    return charstrings[glyphName]


def do_compatibility(vf, master_fonts):
    default_font = vf
    default_charStrings = default_font['CFF '].cff.topDictIndex[0].CharStrings
    glyphOrder = default_font.getGlyphOrder()
    charStrings = [
                font['CFF '].cff.topDictIndex[0].CharStrings for
                font in master_fonts]
    for gname in glyphOrder:

        all_cs = [_get_cs(glyphOrder, cs, gname) for cs in charStrings]
        if len([gs for gs in all_cs if gs is not None]) < 2:
            continue
        # remove the None's from the list.
        cs_list = [cs for cs in all_cs if cs]
        num_masters = len(cs_list)
        default_charstring = default_charStrings[gname]
        compat_pen = CompatibilityPen([], gname, num_masters, 0)
        default_charstring.outlineExtractor = T2OutlineExtractor
        default_charstring.draw(compat_pen)

        # Add the coordinates from all the other regions to the
        # blend lists in the CFF2 charstring.
        region_cs = cs_list[1:]
        for region_idx, region_charstring in enumerate(region_cs, start=1):
            compat_pen.restart(region_idx)
            region_charstring.draw(compat_pen)
        if compat_pen.fixed:
            fixed_cs_list = compat_pen.getCharStrings(
                num_masters, private=default_charstring.private,
                globalSubrs=default_charstring.globalSubrs)
            cs_list = list(cs_list)
            for i, cs in enumerate(cs_list):
                mi = all_cs.index(cs)
                charStrings[mi][gname] = fixed_cs_list[i]
    return


def otfFinder(s):
    return s.replace('.ufo', '.otf')


def add_glyph_names(tt_font, glyph_order):
    postTable = tt_font['post']
    postTable.glyphOrder = tt_font.glyphOrder = glyph_order
    postTable.formatType = 2.0
    postTable.extraNames = []
    postTable.mapping = {}
    postTable.compile(tt_font)


def run(args=None):

    if args is None:
        args = sys.argv[1:]

    options = get_options(args)

    if parse_version(fontToolsVersion) < parse_version("3.19"):
        logger.error("Quitting. The Python fonttools module "
                     "must be at least version 3.41.0")
        return

    if os.path.exists(options.var_font_path):
        os.remove(options.var_font_path)

    designspace = DesignSpaceDocument.fromfile(options.design_space_path)
    ds_data = varLib.load_designspace(designspace)
    master_fonts = varLib.load_masters(designspace, otfFinder)
    logger.progress("Reading source fonts...")
    for i, master_font in enumerate(master_fonts):
        designspace.sources[i].font = master_font

    if options.check_compatibility:
        logger.progress("Checking outline compatibility in source fonts...")
        font_list = [src.font for src in designspace.sources]
        default_font = designspace.sources[ds_data.base_idx].font
        vf = deepcopy(default_font)
        # We copy vf from default_font, because we use VF to hold
        # merged arguments from each source font charstring - this alters
        # the font, which we don't want to do to the default font.
        do_compatibility(vf, font_list)

    logger.progress("Building VF font...")
    # Note that we now pass in the design space object, not a path
    # to the design space file, in order to pass in the 
    # modified sources fonts without having to recompile and save them.
    varFont, _, _ = varLib.build(designspace, otfFinder)

    if options.keep_glyph_names:
        default_font = designspace.sources[ds_data.base_idx].font
        add_glyph_names(varFont, default_font.glyphOrder)

    varFont.save(options.var_font_path)
    logger.progress("Built variable font '%s'" % (options.var_font_path))


if __name__ == '__main__':
    run()
