import setuptools


setuptools.setup(
    name='trackers',
    packages=setuptools.find_packages(),
    include_package_data=True,
    install_requires=[
        'aiohttp>=2.0',
        'aniso8601',
        'asyncio-contextmanager',
        'asynctest',
        'attrs',
        'beautifulsoup4',
        'filemagic',
        'more-itertools',
        'msgpack-python',
        'nvector',
        'python-slugify',
        'pyyaml',
        'uvloop',
    ],
    entry_points={
        'console_scripts': [
            'serve=trackers.serve:main',
            'convert_to_static=trackers.bin_utils:convert_to_static',
            'assign_rider_colors=trackers.bin_utils:assign_rider_colors',
            'add_gpx_to_event_routes=trackers.bin_utils:add_gpx_to_event_routes',
            'reformat_event=trackers.bin_utils:reformat_event',
        ],
    },
    # test_suite='trackers.tests.suite',
)
