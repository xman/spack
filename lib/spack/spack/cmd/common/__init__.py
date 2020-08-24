# Copyright 2013-2020 Lawrence Livermore National Security, LLC and other
# Spack Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import spack.paths
import llnl.util.tty.color as color


def shell_init_instructions():
    return [
        "To set up shell support, run the command below for your shell.",
        "",
        color.colorize("@*c{For bash/zsh/sh:}"),
        "  . %s/setup-env.sh" % spack.paths.share_path,
        "",
        color.colorize("@*c{For csh/tcsh:}"),
        "  source %s/setup-env.csh" % spack.paths.share_path,
        "",
        color.colorize("@*c{For fish:}"),
        "  source %s/setup-env.fish" % spack.paths.share_path,
        "",
    ]
