# Copyright 2013-2019 Lawrence Livermore National Security, LLC and other
# Spack Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from __future__ import print_function

import collections
import pkgutil
import re
import sys
import tempfile
import types
from six import string_types

import llnl.util.tty as tty
import llnl.util.tty.color as color

import spack
import spack.cmd
import spack.spec
import spack.package
import spack.package_prefs
import spack.repo
from spack.util.executable import which
from spack.version import ver


#: max line length for ASP programs in characters
_max_line = 80


def issequence(obj):
    if isinstance(obj, string_types):
        return False
    return isinstance(obj, (collections.Sequence, types.GeneratorType))


def listify(args):
    if len(args) == 1 and issequence(args[0]):
        return list(args[0])
    return list(args)


def packagize(pkg):
    if isinstance(pkg, spack.package.PackageMeta):
        return pkg
    return spack.repo.path.get_pkg_class(pkg)


def specify(spec):
    if isinstance(spec, spack.spec.Spec):
        return spec
    return spack.spec.Spec(spec)


class AspObject(object):
    """Object representing a piece of ASP code."""


def _id(thing):
    """Quote string if needed for it to be a valid identifier."""
    return thing if isinstance(thing, AspObject) else '"%s"' % str(thing)


class AspFunction(AspObject):
    def __init__(self, name):
        self.name = name
        self.args = []

    def __call__(self, *args):
        self.args[:] = args
        return self

    def __str__(self):
        return "%s(%s)" % (
            self.name, ', '.join(_id(arg) for arg in self.args))


class AspAnd(AspObject):
    def __init__(self, *args):
        args = listify(args)
        self.args = args

    def __str__(self):
        s = ", ".join(str(arg) for arg in self.args)
        return s


class AspOr(AspObject):
    def __init__(self, *args):
        args = listify(args)
        self.args = args

    def __str__(self):
        return " | ".join(str(arg) for arg in self.args)


class AspNot(AspObject):
    def __init__(self, arg):
        self.arg = arg

    def __str__(self):
        return "not %s" % self.arg


class AspOneOf(AspObject):
    def __init__(self, *args):
        args = listify(args)
        self.args = args

    def __str__(self):
        body = "; ".join(str(arg) for arg in self.args)
        return "1 { %s } 1" % body


class AspFunctionBuilder(object):
    def __getattr__(self, name):
        return AspFunction(name)


fn = AspFunctionBuilder()


class AspGenerator(object):
    def __init__(self, out):
        self.out = out
        self.func = AspFunctionBuilder()
        self.possible_versions = {}

    def title(self, name, char):
        self.out.write('\n')
        self.out.write("%" + (char * 76))
        self.out.write('\n')
        self.out.write("%% %s\n" % name)
        self.out.write("%" + (char * 76))
        self.out.write('\n')

    def h1(self, name):
        self.title(name, "=")

    def h2(self, name):
        self.title(name, "-")

    def section(self, name):
        self.out.write("\n")
        self.out.write("%\n")
        self.out.write("%% %s\n" % name)
        self.out.write("%\n")

    def one_of(self, *args):
        return AspOneOf(*args)

    def _or(self, *args):
        return AspOr(*args)

    def _and(self, *args):
        return AspAnd(*args)

    def _not(self, arg):
        return AspNot(arg)

    def fact(self, head):
        """ASP fact (a rule without a body)."""
        self.out.write("%s.\n" % head)

    def rule(self, head, body):
        """ASP rule (an implication)."""
        rule_line = "%s :- %s.\n" % (head, body)
        if len(rule_line) > _max_line:
            rule_line = re.sub(r' \| ', "\n| ", rule_line)
        self.out.write(rule_line)

    def constraint(self, body):
        """ASP integrity constraint (rule with no head; can't be true)."""
        self.out.write(":- %s.\n" % body)

    def pkg_version_rules(self, pkg):
        """Output declared versions of a package.

        This uses self.possible_versions so that we include any versions
        that arise from a spec.
        """
        pkg = packagize(pkg)
        for v in self.possible_versions[pkg.name]:
            self.fact(fn.version_declared(pkg.name, v))

    def spec_versions(self, spec):
        """Return list of clauses expressing spec's version constraints."""
        spec = specify(spec)
        assert spec.name

        if spec.concrete:
            return [fn.version(spec.name, spec.version)]

        # version must be *one* of the ones the spec allows.
        allowed_versions = [
            v for v in self.possible_versions[spec.name]
            if v.satisfies(spec.versions)
        ]
        predicates = [fn.version(spec.name, v) for v in allowed_versions]

        # conflict with any versions that do not satisfy the spec
        if predicates:
            return [self.one_of(*predicates)]
        return []

    def compiler_defaults(self):
        """Facts about available compilers."""
        compilers = spack.compilers.all_compiler_specs()

        compiler_versions = collections.defaultdict(lambda: set())
        for compiler in compilers:
            compiler_versions[compiler.name].add(compiler.version)

        for compiler in compiler_versions:
            self.fact(fn.compiler(compiler))
            self.rule(
                self._or(
                    fn.compiler_version(compiler, v)
                    for v in sorted(compiler_versions[compiler])),
                fn.compiler(compiler))

    def package_compiler_defaults(self, pkg):
        """Add facts about the default compiler.

        TODO: integrate full set of preferences into the solve (this only
        TODO: considers the top preference)
        """
        # get list of all compilers
        compiler_list = spack.compilers.all_compiler_specs()
        if not compiler_list:
            raise spack.compilers.NoCompilersError()

        # prefer package preferences, then latest version
        ppk = spack.package_prefs.PackagePrefs(pkg.name, 'compiler')
        compiler_list = sorted(
            compiler_list, key=lambda x: (x.name, x.version), reverse=True)
        compiler_list = sorted(compiler_list, key=ppk)

        # write out default rules for this package's compilers
        default_compiler = compiler_list[0]
        self.fact(fn.node_compiler_default(pkg.name, default_compiler.name))
        self.fact(fn.node_compiler_default_version(
            pkg.name, default_compiler.name, default_compiler.version))

    def pkg_rules(self, pkg):
        pkg = packagize(pkg)

        # versions
        self.pkg_version_rules(pkg)
        self.out.write('\n')

        # variants
        for name, variant in pkg.variants.items():
            self.rule(fn.variant(pkg.name, name),
                      fn.node(pkg.name))

            single_value = not variant.multi
            single = fn.variant_single_value(pkg.name, name)
            if single_value:
                self.rule(single, fn.node(pkg.name))
                self.rule(
                    fn.variant_default_value(pkg.name, name, variant.default),
                    fn.node(pkg.name))
            else:
                self.rule(self._not(single), fn.node(pkg.name))
                defaults = variant.default.split(',')
                for val in defaults:
                    self.rule(
                        fn.variant_default_value(pkg.name, name, val),
                        fn.node(pkg.name))
            self.out.write('\n')

        # default compilers for this package
        self.package_compiler_defaults(pkg)

        # dependencies
        for name, conditions in pkg.dependencies.items():
            for cond, dep in conditions.items():
                self.fact(fn.depends_on(dep.pkg.name, dep.spec.name))

    def spec_clauses(self, spec):
        """Return a list of clauses the spec mandates are true.

        Arguments:
            spec (Spec): the spec to analyze
        """
        clauses = []

        if spec.name:
            clauses.append(fn.node(spec.name))

        clauses.extend(self.spec_versions(spec))

        # seed architecture at the root (we'll propagate later)
        # TODO: use better semantics.
        arch = spec.architecture
        if arch:
            if arch.platform:
                clauses.append(fn.arch_platform_set(spec.name, arch.platform))
            if arch.os:
                clauses.append(fn.arch_os_set(spec.name, arch.os))
            if arch.target:
                clauses.append(fn.arch_target_set(spec.name, arch.target))

        # variants
        for vname, variant in spec.variants.items():
            clauses.append(fn.variant_set(spec.name, vname, variant.value))

        # compiler and compiler version
        if spec.compiler:
            clauses.append(fn.node_compiler_set(spec.name, spec.compiler.name))
            if spec.compiler.concrete:
                clauses.append(fn.node_compiler_version_set(
                    spec.name, spec.compiler.name, spec.compiler.version))

        # TODO
        # dependencies
        # external_path
        # external_module
        # compiler_flags
        # namespace

        return clauses

    def build_version_dict(self, possible_pkgs, specs):
        """Declare any versions in specs not declared in packages."""
        self.possible_versions = collections.defaultdict(lambda: set())

        for pkg_name in possible_pkgs:
            pkg = spack.repo.get(pkg_name)
            for v in pkg.versions:
                self.possible_versions[pkg_name].add(v)

        for spec in specs:
            for dep in spec.traverse():
                if dep.versions.concrete:
                    self.possible_versions[dep.name].add(dep.version)

    def arch_defaults(self):
        """Add facts about the default architecture for a package."""
        self.h2('Default architecture')
        default_arch = spack.spec.ArchSpec(spack.architecture.sys_type())
        self.fact(fn.arch_platform_default(default_arch.platform))
        self.fact(fn.arch_os_default(default_arch.os))
        self.fact(fn.arch_target_default(default_arch.target))

    def virtual_providers(self, virtuals):
        for vspec in virtuals:
            providers = spack.repo.path.providers_for(vspec)
            print("PROVIDE", providers, [type(t) for t in providers])

    def generate_asp_program(self, specs):
        """Write an ASP program for specs.

        Writes to this AspGenerator's output stream.

        Arguments:
            specs (list): list of Specs to solve
        """
        # get list of all possible dependencies
        pkg_names = set(spec.fullname for spec in specs)

        possible = set()
        for name in pkg_names:
            pkg = spack.repo.path.get_pkg_class(name)
            possible.update(pkg.possible_dependencies())

        pkgs = set(possible) | set(pkg_names)

        concretize_lp = pkgutil.get_data('spack.solver', 'concretize.lp')
        self.out.write(concretize_lp.decode("utf-8"))

        # traverse all specs and packages to build dict of possible versions
        self.build_version_dict(possible, specs)

        self.h1('General Constraints')
        self.compiler_defaults()
        self.arch_defaults()
        self.virtual_providers(virtuals)

        self.h1('Package Constraints')
        for pkg in pkgs:
            self.h2('Package: %s' % pkg)
            self.pkg_rules(pkg)

        self.h1('Spec Constraints')
        for spec in specs:
            for dep in spec.traverse():
                self.h2('Spec: %s' % str(dep))
                self.fact(fn.node(dep.name))
                for clause in self.spec_clauses(dep):
                    self.rule(clause, fn.node(dep.name))

        self.out.write('\n')
        display_lp = pkgutil.get_data('spack.solver', 'display.lp')
        self.out.write(display_lp.decode("utf-8"))


class ResultParser(object):
    """Class with actions that can re-parse a spec from ASP."""
    def __init__(self):
        self._result = None

    def node(self, pkg):
        if pkg not in self._specs:
            self._specs[pkg] = spack.spec.Spec(pkg)

    def _arch(self, pkg):
        arch = self._specs[pkg].architecture
        if not arch:
            arch = spack.spec.ArchSpec()
            self._specs[pkg].architecture = arch
        return arch

    def arch_platform(self, pkg, platform):
        self._arch(pkg).platform = platform

    def arch_os(self, pkg, os):
        self._arch(pkg).os = os

    def arch_target(self, pkg, target):
        self._arch(pkg).target = target

    def variant_value(self, pkg, name, value):
        pkg_class = spack.repo.path.get_pkg_class(pkg)
        variant = pkg_class.variants[name].make_variant(value)
        self._specs[pkg].variants[name] = variant

    def version(self, pkg, version):
        self._specs[pkg].versions = ver([version])

    def node_compiler(self, pkg, compiler):
        self._specs[pkg].compiler = spack.spec.CompilerSpec(compiler)

    def node_compiler_version(self, pkg, compiler, version):
        self._specs[pkg].compiler.versions = spack.version.VersionList(
            [version])

    def depends_on(self, pkg, dep):
        self._specs[pkg]._add_dependency(
            self._specs[dep], ('link', 'run'))

    def parse(self, stream, result):
        for line in stream:
            match = re.match(r'SATISFIABLE', line)
            if match:
                result.satisfiable = True
                continue

            match = re.match(r'UNSATISFIABLE', line)
            if match:
                result.satisfiable = False
                continue

            match = re.match(r'Answer: (\d+)', line)
            if not match:
                continue

            answer_number = int(match.group(1))
            assert answer_number == len(result.answers) + 1

            answer = next(stream)
            tty.debug("Answer: %d" % answer_number, answer)

            self._specs = {}
            for m in re.finditer(r'(\w+)\(([^)]*)\)', answer):
                name, arg_string = m.groups()
                args = re.findall(r'"([^"]*)"', arg_string)

                action = getattr(self, name)
                assert action and callable(action)
                action(*args)

            result.answers.append(self._specs)


class Result(object):
    def __init__(self, asp):
        self.asp = asp
        self.satisfiable = None
        self.warnings = None
        self.answers = []


def highlight(string):
    """Syntax highlighting for ASP programs"""
    # variables
    string = re.sub(r'\b([A-Z])\b', r'@y{\1}', string)

    # implications
    string = re.sub(r':-', r'@*G{:-}', string)

    # final periods
    string = re.sub(r'^([^%].*)\.$', r'\1@*G{.}', string, flags=re.MULTILINE)

    # directives
    string = re.sub(
        r'(#\w*)( (?:\w*)?)((?:/\d+)?)', r'@*B{\1}@c{\2}\3', string)

    # functions
    string = re.sub(r'(\w[\w-]+)\(([^)]*)\)', r'@C{\1}@w{(}\2@w{)}', string)

    # comments
    string = re.sub(r'(%.*)$', r'@w\1@.', string, flags=re.MULTILINE)

    # strings
    string = re.sub(r'("[^"]*")', r'@m{\1}', string)

    # result
    string = re.sub(r'\bUNSATISFIABLE', "@R{UNSATISFIABLE}", string)
    string = re.sub(r'\bINCONSISTENT', "@R{INCONSISTENT}", string)
    string = re.sub(r'\bSATISFIABLE', "@G{SATISFIABLE}", string)

    return string


#
# These are handwritten parts for the Spack ASP model.
#
def solve(specs, dump=None, models=1):
    """Solve for a stable model of specs.

    Arguments:
        specs (list): list of Specs to solve.
        dump (tuple): what to dump
        models (int): number of satisfying specs to find (default: 1)
    """
    clingo = which('clingo', required=True)
    parser = ResultParser()

    def colorize(string):
        color.cprint(highlight(color.cescape(string)))

    with tempfile.TemporaryFile("w+") as program:
        generator = AspGenerator(program)
        generator.generate_asp_program(specs)
        program.seek(0)

        result = Result(program.read())
        program.seek(0)

        if 'asp' in dump:
            if sys.stdout.isatty():
                tty.msg('ASP program:')
            colorize(result.asp)

        with tempfile.TemporaryFile("w+") as output:
            with tempfile.TemporaryFile() as warnings:
                clingo(
                    '--models=%d' % models,
                    input=program,
                    output=output,
                    error=warnings,
                    fail_on_error=False)

                warnings.seek(0)
                result.warnings = warnings.read()

                # dump any warnings generated by the solver
                if 'warnings' in dump:
                    if result.warnings:
                        if sys.stdout.isatty():
                            tty.msg('Clingo gave the following warnings:')
                        colorize(result.warnings)
                    else:
                        if sys.stdout.isatty():
                            tty.msg('No warnings.')

                output.seek(0)
                result.output = output.read()

                # dump the raw output of the solver
                if 'output' in dump:
                    if sys.stdout.isatty():
                        tty.msg('Clingo output:')
                    colorize(result.output)

                output.seek(0)
                parser.parse(output, result)

    return result
