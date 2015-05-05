#!/usr/bin/env python
# Copyright (C) 2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import argparse
import fnmatch
import logging
import os
import platform
import sys
import yaml

from six.moves import configparser

from jenkins_jobs.builder import Builder
from jenkins_jobs.cli.subcommands import update as cli_update
from jenkins_jobs.cli.subcommands import delete as cli_delete
from jenkins_jobs.cli.subcommands import test as cli_test
from jenkins_jobs.cli.subcommands import delete_all as cli_delete_all
import jenkins_jobs.cli
import jenkins_jobs.config
from jenkins_jobs.errors import JenkinsJobsException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()


def confirm(question):
    answer = raw_input('%s (Y/N): ' % question).upper().strip()
    if not answer == 'Y':
        sys.exit('Aborted')


def recurse_path(root, excludes=None):
    if excludes is None:
        excludes = []

    basepath = os.path.realpath(root)
    pathlist = [basepath]

    patterns = [e for e in excludes if os.path.sep not in e]
    absolute = [e for e in excludes if os.path.isabs(e)]
    relative = [e for e in excludes if os.path.sep in e and
                not os.path.isabs(e)]
    for root, dirs, files in os.walk(basepath, topdown=True):
        dirs[:] = [
            d for d in dirs
            if not any([fnmatch.fnmatch(d, pattern) for pattern in patterns])
            if not any([fnmatch.fnmatch(os.path.abspath(os.path.join(root, d)),
                                        path)
                        for path in absolute])
            if not any([fnmatch.fnmatch(os.path.relpath(os.path.join(root, d)),
                                        path)
                        for path in relative])
        ]
        pathlist.extend([os.path.join(root, path) for path in dirs])

    return pathlist


def create_parser():

    parser = argparse.ArgumentParser()
    parser, recursive_parser = jenkins_jobs.cli.parse(parser, [])

    subparser = parser.add_subparsers(help='update, test or delete job',
                                      dest='command')

    cli_update.parse(subparser, [recursive_parser])

    cli_test.parse(subparser, [recursive_parser])

    cli_delete.parse(subparser, [recursive_parser])

    cli_delete_all.parse(subparser, [recursive_parser])

    return parser


def main(argv=None):

    # We default argv to None and assign to sys.argv[1:] below because having
    # an argument default value be a mutable type in Python is a gotcha. See
    # http://bit.ly/1o18Vff
    if argv is None:
        argv = sys.argv[1:]

    parser = create_parser()
    options = parser.parse_args(argv)
    if not options.command:
        parser.error("Must specify a 'command' to be performed")
    if (options.log_level is not None):
        options.log_level = getattr(logging, options.log_level.upper(),
                                    logger.getEffectiveLevel())
        logger.setLevel(options.log_level)

    config = jenkins_jobs.config.load(options)
    execute(options, config)


def munge_config_options(options, config):
    logger.debug("Config: {0}".format(config))

    # check the ignore_cache setting: first from command line,
    # if not present check from ini file
    ignore_cache = False
    if options.ignore_cache:
        ignore_cache = options.ignore_cache
    elif config.has_option('jenkins', 'ignore_cache'):
        logging.warn('ignore_cache option should be moved to the [job_builder]'
                     ' section in the config file, the one specified in the '
                     '[jenkins] section will be ignored in the future')
        ignore_cache = config.getboolean('jenkins', 'ignore_cache')
    elif config.has_option('job_builder', 'ignore_cache'):
        ignore_cache = config.getboolean('job_builder', 'ignore_cache')

    # workaround for python 2.6 interpolation error
    # https://bugs.launchpad.net/openstack-ci/+bug/1259631
    try:
        user = config.get('jenkins', 'user')
    except (TypeError, configparser.NoOptionError):
        user = None
    try:
        password = config.get('jenkins', 'password')
    except (TypeError, configparser.NoOptionError):
        password = None

    plugins_info = None

    if getattr(options, 'plugins_info_path', None) is not None:
        with open(options.plugins_info_path, 'r') as yaml_file:
            plugins_info = yaml.load(yaml_file)
        if not isinstance(plugins_info, list):
            raise JenkinsJobsException("{0} must contain a Yaml list!"
                                       .format(options.plugins_info_path))
    elif (not options.conf or not
          config.getboolean("jenkins", "query_plugins_info")):
        logger.debug("Skipping plugin info retrieval")
        plugins_info = {}

    if options.allow_empty_variables is not None:
        config.set('job_builder',
                   'allow_empty_variables',
                   str(options.allow_empty_variables))

    if getattr(options, 'path', None):
        if options.path == sys.stdin:
            logger.debug("Input file is stdin")
            if options.path.isatty():
                key = 'CTRL+Z' if platform.system() == 'Windows' else 'CTRL+D'
                logger.warn(
                    "Reading configuration from STDIN. Press %s to end input.",
                    key)

        # take list of paths
        options.path = options.path.split(os.pathsep)

        do_recurse = (getattr(options, 'recursive', False) or
                      config.getboolean('job_builder', 'recursive'))

        excludes = [e for elist in options.exclude
                    for e in elist.split(os.pathsep)] or \
            config.get('job_builder', 'exclude').split(os.pathsep)
        paths = []
        for path in options.path:
            if do_recurse and os.path.isdir(path):
                paths.extend(recurse_path(path, excludes))
            else:
                paths.append(path)
        options.path = paths

    builder = Builder(config.get('jenkins', 'url'),
                      user,
                      password,
                      config,
                      ignore_cache=ignore_cache,
                      flush_cache=options.flush_cache,
                      plugins_list=plugins_info)

    return builder, options, config


def execute(options, config):

    builder, options, config = munge_config_options(options, config)

    if options.command == 'delete':
        for job in options.name:
            builder.delete_job(job, options.path)
    elif options.command == 'delete-all':
        confirm('Sure you want to delete *ALL* jobs from Jenkins server?\n'
                '(including those not managed by Jenkins Job Builder)')
        logger.info("Deleting all jobs")
        builder.delete_all_jobs()
    elif options.command == 'update':
        if options.n_workers < 0:
            raise JenkinsJobsException(
                'Number of workers must be equal or greater than 0')

        logger.info("Updating jobs in {0} ({1})".format(
            options.path, options.names))
        num_updated_jobs = builder.update_jobs(
            options.path, options.names,
            n_workers=options.n_workers)
        logger.info("Number of jobs updated: %d", num_updated_jobs)
        if options.delete_old:
            num_deleted_jobs = builder.delete_old_managed()
            logger.info("Number of jobs deleted: %d", num_deleted_jobs)
    elif options.command == 'test':
        builder.update_jobs(options.path, options.name,
                            output=options.output_dir,
                            n_workers=1)


if __name__ == '__main__':
    sys.path.insert(0, '.')
    main()
