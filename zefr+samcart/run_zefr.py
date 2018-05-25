import sys
import os
import time as Time

HOME_DIR = '/home/sitaramanja/code/zefr-integration/'

TIOGA_DIR = HOME_DIR + '/tioga-HighOrderArtBnd/'
ZEFR_DIR = HOME_DIR + '/zefr-swig/'
SAMCART_DIR = HOME_DIR + '/SAMCart/'

sys.path.append(TIOGA_DIR+'/bin/')
sys.path.append(TIOGA_DIR+'/run/')
sys.path.append(ZEFR_DIR+'/bin/')
sys.path.append(ZEFR_DIR+'/run/')
sys.path.append(SAMCART_DIR)

from mpi4py import MPI
import numpy as np

from zefrInterface import zefrSolver
from tiogaInterface import Tioga
try:
 from samcartInterface import samcartSolver
except:
 from samcartStandIn import samcartSolver

Comm = MPI.COMM_WORLD
rank = Comm.Get_rank()
nproc = Comm.Get_size()

# ------------------------------------------------------------
# Parse our input
# ------------------------------------------------------------

if len(sys.argv) < 2:
    print("Usage:")
    print("  {0} <inputfile> <nRankGrid1> <nRankGrid2> ...".format(sys.argv[0]))
    exit

nGrids = 1
gridID = 0
inputFile = sys.argv[1]

# Split our run into the various grids
if len(sys.argv) > 2:
    nGrids = len(sys.argv) - 2
    nRanksGrid = []
    rankSum = 0
    for i in range(0,nGrids):
        rankSum += int(sys.argv[i+2])
        if rankSum > rank:
            gridID = i;
            break;

if nGrids > 1:
    gridComm = Comm.Split(gridID,rank)
else:
    gridID = 0
    gridComm = Comm

gridRank = gridComm.Get_rank()
gridSize = gridComm.Get_size()

# Read in overall simulation parameters
parameters = {}
with open(inputFile) as f:
    for line in f:
        line = line.strip().split()
        if len(line) == 2 and not str(line[0]).startswith('#'):
            try:
                parameters[line[0]] = float(line[1])
            except:
                parameters[line[0]] = line[1]

integer_vals = ['obesubsteps', 'nsave', 'plot-freq', 'report-freq',
    'restart-freq', 'force-freq']
for val in integer_vals:
  try:
    parameters[val] = int(parameters[val])
  except:
    pass

expected_conditions = ['meshRefLength','reyNumber','reyRefLength','refMach',
    'dt','Mach','viscosity','gamma','prandtl','from_restart','moving-grid']

conditions = {}
for cond in expected_conditions:
    try:
        conditions[cond] = parameters[cond]
    except:
        if rank == 0:
            print('Condition',cond,'not given in',inputFile)

# ------------------------------------------------------------
# Begin setting up TIOGA and the ZEFR solvers
# ------------------------------------------------------------

try:
  zefrInput = parameters['zefrInput']
except:
  print('ZEFR input file ("zefrInput") not given in',inputFile)

ZEFR    = zefrSolver(zefrInput,gridID,nGrids)
TIOGA   = Tioga(gridID,nGrids)
SAMCART = samcartSolver()

dt = parameters['dt']

nSteps = int(parameters['nsteps'])
nStages = int(parameters['nstages'])

moving = parameters['moving-grid'] == 1
viscous = parameters['viscous'] == 1

try:
    repFreq = int(parameters['report-freq'])
except:
    repFreq = 0
    parameters['report-freq'] = 0
    if rank == 0:
        print('Parameter report-freq not found; disabling residual reporting.')

try:
    plotFreq = int(parameters['plot-freq'])
except:
    plotFreq = 0
    parameters['plot-freq'] = 0
    if rank == 0:
        print('Parameter plot-freq not found; disabling plotting.')

try:
    restartFreq = int(parameters['restart-freq'])
except:
    restartFreq = 0
    parameters['restart-freq'] = 0
    if rank == 0:
        print('Parameter restart-freq not found; disabling SAMCart restart file writing.')

try:
    forceFreq = int(parameters['force-freq'])
except:
    forceFreq = 0
    parameters['force-freq'] = 0
    if rank == 0:
        print('Parameter force-freq not found; disabling force calculation.')

# Process high-level simulation inputs
ZEFR.sifInitialize(parameters, conditions)
TIOGA.sifInitialize(parameters, conditions)
SAMCART.initialize('samcart/input.samcart',parameters)
SAMCART.sifInitialize(parameters,parameters)

# Setup the ZEFR solver - read the grid, initialize the solution, etc.
gridData, callbacks = ZEFR.initData()

# Setup TIOGA - give it the grid and callback info needed for connectivity
TIOGA.initData(ZEFR.gridData, ZEFR.callbacks)

# ------------------------------------------------------------
# Grid Preprocessing
# ------------------------------------------------------------
TIOGA.preprocess()
TIOGA.performConnectivity()
TIOGA.initIGBPs()
SAMCART.setIGBPs(TIOGA.igbpdata)
SAMCART.initData()
TIOGA.initAMRData(SAMCART.gridData)
TIOGA.performAMRConnectivity()

# ------------------------------------------------------------
# Restarting (if requested)
# ------------------------------------------------------------
if parameters['from_restart'] == 'yes':
    initIter = int(parameters['restartstep'])
    time = parameters['restart-time']
    ZEFR.restart(initIter)
    if parameters['moving-grid']:
        ZEFR.deformPart1(time+dt,initIter)
        TIOGA.unblankPart1()
        ZEFR.deformPart2(time,initIter)
        TIOGA.unblankPart2()

        TIOGA.initIGBPs()
        SAMCART.setIGBPs(TIOGA.igbpdata)
        TIOGA.performAMRConnectivity()

        if parameters['use-gpu']:
            ZEFR.updateBlankingGpu()
else:
    initIter = 0
    time = 0.0

iter = initIter

ZEFR.writePlotData(iter)
SAMCART.writePlotData(iter)

# Initialize interpolated fringe node data
TIOGA.exchangeSolutionAMR()

# ------------------------------------------------------------
# Run the simulation
# ------------------------------------------------------------
for i in range(iter+1,nSteps+1):
    # Do unblanking here 
    # (move to t+dt, hole cut, move to t, hole cut, union on iblank)
    if moving:
        ZEFR.deformPart1(time+dt,i)
        TIOGA.unblankPart1()
        ZEFR.deformPart2(time,i)
        TIOGA.unblankPart2()        # Set final iblank & do unblanking if needed

        TIOGA.performPointConnectivity()

        nadapt = 0  # TODO
        if nadapt > 0 and mod(i,nadapt)==0:
            TIOGA.initIGBPs()
            SAMCART.setIGBPs(TIOGA.igbpdata)
            SAMCART.adapt()

        TIOGA.performConnectivityAMR()

    start = Time.clock_gettime(0)
    for j in range(0,nStages):
        # Move grids
        if moving and j != 0:
            ZEFR.moveGrid(i,j)
            TIOGA.performPointConnectivity()

        # Have ZEFR extrapolate solution first so it won't overwrite interpolated data
        ZEFR.runSubStepStart(i,j)

        # Interpolate solution
        TIOGA.exchangeSolutionAMR()

        # Calculate first part of residual, up to corrected gradient
        ZEFR.runSubStepMid(i,j)

        # Finish residual calculation and RK stage advancement
        # (Should include rigid_body_update() if doing 6DOF from ZEFR)
        ZEFR.runSubStepFinish(i,j)
    stop = Time.clock_gettime(0)
    #if rank == 0:
    #    print('ZEFR step wall time: {:3.2e}'.format(stop-start))
    start = Time.clock_gettime(0)
    TIOGA.exchangeSolutionAMR() # Interpolate 't^{n+1}' data from ZEFR
    SAMCART.runSubSteps(i)
    stop = Time.clock_gettime(0)
    #if rank == 0:
    #    print('SAMCart step wall time: {:3.2e}'.format(stop-start))
    
    time += dt

    if repFreq > 0 and (i % repFreq == 0 or i == nSteps or i == initIter+1):
        ZEFR.reportResidual(i)

    Plot = False
    if plotFreq > 0 and (i % plotFreq == 0 or i == nSteps):
        Plot = True
        ZEFR.writePlotData(i)
        SAMCART.writePlotData(i)

    if restartFreq > 0 and (i % restartFreq == 0 or i == nSteps):
        if not Plot:
            # ZEFR plot files are also restart files - don't save twice
            ZEFR.writePlotData(i)
        try:
            SAMCART.writeRestartData(i)
        except:
            pass

    if forceFreq > 0 and (i % forceFreq == 0 or i == nSteps):
        ZEFR.computeForces(i)
        forces = ZEFR.getForcesAndMoments()
        if rank == 0:
            print('Iter {0}: Forces {1}'.format(i,forces))

# ------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------

TIOGA.finish()
SAMCART.finish()

if rank == 0:
    print(" ***************** RUN COMPLETE ***************** ")
