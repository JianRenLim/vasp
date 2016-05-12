"""
code for running NEB calculations in Vasp

here is typical code to set up the band:

calc = Vasp('../surfaces/Pt-slab-O-fcc')
initial_atoms = calc.get_atoms()

calc = Vasp('../surfaces/Pt-slab-O-hcp')
final_atoms = calc.get_atoms()

images = [initial_atoms]
images += [initial_atoms.copy() for i in range(3)]
images += [final_atoms]

neb = NEB(images)
# Interpolate linearly the positions of the three middle images:
neb.interpolate()

calc = Vasp('O-diffusion',
            ibrion=2,
            nsw=50,
            images=5,  # initial + nimages + final
            spring=-5,
            atoms=images)
images, energies = calc.get_neb()

The spring tag triggers the setup of an NEB calculation for Vasp.

"""

import os
import numpy as np

from ase.io import read
from ase.io.vasp import write_vasp
from ase.calculators.vasp import Vasp

import vasp
from vasp import log
from monkeypatch import monkeypatch_class
from vasprc import VASPRC


@monkeypatch_class(vasp.Vasp)
def get_neb(self, npi=1):
    """Returns images, energies if available or runs the job.

    npi = cores per image for running the calculations. Default=1

    show: if True show an NEB plot
    """

    calc_required = False

    # check for OUTCAR in each image dir
    for i in range(1, len(self.neb) - 1):
        wf = '{0}/OUTCAR'.format(str(i).zfill(2))
        wf = os.path.join(self.directory, wf)
        if not os.path.exists(wf):
            calc_required = True
            break
        else:
            # there was an OUTCAR, now we need to check for
            # convergence.
            done = False
            with open(wf) as f:
                for line in f:
                    if ('reached required accuracy - stopping structural'
                        ' energy minimisation') in line:
                        done = True
                        break
            if not done:
                calc_required = True
                break

    if calc_required:
        # this creates the directories and files if needed.  write out
        # all the images, including initial and final
        if not os.path.isdir(self.directory):
            os.makedirs(self.directory)

        self.write_incar()
        self.write_kpoints()
        self.write_potcar()
        self.write_metadata()

        for i, atoms in enumerate(self.neb):
              # zero-padded directory name
            image_dir = os.path.join(self.directory, str(i).zfill(2))
            if not os.path.isdir(image_dir):
                # create if needed.
                os.makedirs(image_dir)
                write_vasp('{0}/POSCAR'.format(image_dir),
                           atoms[self.resort],
                           symbol_count=self.symbol_count)
        with open(os.path.join(self.directory,
                               '00/energy'), 'w') as f:
            f.write(str(self.neb[0].get_potential_energy()))

        with open(os.path.join(self.directory,
                               '0{}/energy'.format(len(atoms) - 1)),
                  'w') as f:
            f.write(str(self.neb[-1].get_potential_energy()))

        VASPRC['queue.ppn'] = npi * (len(self.neb) - 2)
        log.debug('Running on %i cores', VASPRC['queue.ppn'])

        self.calculate()  # this will raise VaspSubmitted

    #############################################
    # now we are just retrieving results
    # this is a tricky point. unless the calc stores an absolute path,
    # it may be tricky to call get_potential energy

    energies = []
    with open(os.path.join(self.directory,
                           '00/energy')) as f:
        energies += [float(f.read())]

    import ase.io
    for i in range(1, len(self.neb) - 1):
        atoms = ase.io.read(os.path.join(self.directory,
                                         str(i).zfill(2),
                                         'CONTCAR'))[self.resort]
        self.neb[i].positions = atoms.positions
        self.neb[i].cell = atoms.cell

        energy = None
        with open(os.path.join(self.directory,
                               str(i).zfill(2),
                               'OUTCAR')) as f:
            for line in f:
                if 'free energy    TOTEN  =' in line:
                    energy = float(line.split()[4])

        energies += [energy]

    with open(os.path.join(self.directory,
                           '0{}/energy'.format(len(self.neb) - 1))) as f:
        energies += [float(f.read())]

    energies = np.array(energies)
    energies -= energies[0]

    return (self.neb, np.array(energies))


@monkeypatch_class(vasp.Vasp)
def plot_neb(self, show=True):
    """Return a list of the energies and atoms objects for each image in

    the band.

    by default shows the plot figure
    """
    images, energies = self.get_neb()
    # add fitted line to band energies. we make a cubic spline
    # interpolating function of the negative energy so we can find the
    # minimum which corresponds to the barrier
    from scipy.interpolate import interp1d
    from scipy.optimize import fmin
    f = interp1d(range(len(energies)),
                 -energies,
                 kind='cubic', bounds_error=False)
    x0 = len(energies) / 2.  # guess barrier is at half way
    xmax = fmin(f, x0)

    xfit = np.linspace(0, len(energies) - 1)
    bandfit = -f(xfit)

    import matplotlib.pyplot as plt
    p = plt.plot(energies-energies[0], 'bo ', label='images')
    plt.plot(xfit, bandfit, 'r-', label='fit')
    plt.plot(xmax, -f(xmax), '* ', label='max')
    plt.xlabel('Image')
    plt.ylabel('Energy (eV)')
    s = ['$\Delta E$ = {0:1.3f} eV'.format(float(energies[-1]-energies[0])),
         '$E^\ddag$ = {0:1.3f} eV'.format(float(-f(xmax)))]

    plt.title('\n'.join(s))
    plt.legend(loc='best', numpoints=1)
    if show:
        from ase.visualize import view
        view(images)
        plt.show()
    return p


def read_neb_calculator():
    """Read calculator from the current working directory.

    Static method that returns a :mod:`jasp.Jasp` calculator.
    """
    log.debug('Entering read_neb_calculator in {0}'.format(os.getcwd()))

    calc = Vasp()
    calc.vaspdir = os.getcwd()
    calc.read_incar()
    calc.read_kpoints()

    # set default functional
    # if both gga and xc are not specified
    if calc.string_params['gga'] is None:
        if calc.input_params['xc'] is None:
            calc.input_params['xc'] = 'PBE'

    images = []
    log.debug('calc.int_params[images] = %i', calc.int_params['images'])
    # Add 2 to IMAGES flag from INCAR to get
    # first and last images
    for i in range(calc.int_params['images'] + 2):
        log.debug('reading neb calculator: 0%i', i)
        cwd = os.getcwd()

        os.chdir('{0}'.format(str(i).zfill(2)))
        if os.path.exists('CONTCAR'):
            f = open('CONTCAR')
            if f.read() == '':
                log.debug('CONTCAR was empty, vasp probably still running')
                fname = 'POSCAR'
            else:
                fname = 'CONTCAR'
        else:
            fname = 'POSCAR'

        atoms = read(fname, format='vasp')

        f = open('ase-sort.dat')
        sort, resort = [], []
        for line in f:
            s, r = [int(x) for x in line.split()]
            sort.append(s)
            resort.append(r)

        images += [atoms[resort]]
        os.chdir(cwd)

    log.debug('len(images) = %i', len(images))

    f = open('00/energy')
    calc.neb_initial_energy = float(f.readline().strip())
    f.close()
    f = open('{0}/energy'.format(str(len(images) - 1).zfill(2)))
    calc.neb_final_energy = float(f.readline().strip())
    f.close()

    calc.neb_images = images
    calc.neb_nimages = len(images) - 2
    calc.neb = True
    return calc
