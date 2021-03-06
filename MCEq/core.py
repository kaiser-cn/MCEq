# -*- coding: utf-8 -*-
"""
:mod:`MCEq.core` - core module
==============================

This module contains the main program features. Instantiating :class:`MCEq.core.MCEqRun`
will initialize the data structures and particle tables, create and fill the
interaction and decay matrix and check if all information for the caclulation
of inclusive fluxes in the atmosphere is available.

The preferred way to instantiate :class:`MCEq.core.MCEqRun` is::

    from mceq_config import config
    from MCEq.core import MCEqRun
    import CRFluxModels as pm

    mceq_run = MCEqRun(interaction_model='SIBYLL2.1',
                       primary_model=(pm.HillasGaisser2012, "H3a"),
                       **config)

    mceq_run.set_theta_deg(60.)
    mceq_run.solve()

"""

import numpy as np
from time import time
from mceq_config import dbg, config

class MCEqRun():
    """Main class for handling the calclation.

    This class is the main user interface for the caclulation. It will
    handle initialization and various error/configuration checks. The
    setup has to be accomplished before invoking the integration routine
    is :func:`MCeqRun.solve`. Changes of configuration, such as:

    - interaction model in :meth:`MCEqRun.set_interaction_model`,
    - primary flux in :func:`MCEqRun.set_primary_model`,
    - zenith angle in :func:`MCEqRun.set_theta_deg`,
    - density profile in :func:`MCEqRun.set_atm_model`,
    - member particles of the special ``obs_`` group in :func:`MCEqRun.set_obs_particles`,

    can be made on an active instance of this class, while calling
    :func:`MCEqRun.solve` subsequently to calculate the solution
    corresponding to the settings.

    The result can be retrieved by calling :func:`MCEqRun.get_solution`.


    Args:
      interaction_model (string): PDG ID of the particle
      atm_model (string,sting,string): model type, location, season
      primary_model (class, param_tuple): classes derived from
        :class:`CRFluxModels.PrimaryFlux` and its parameters as tuple
      theta_deg (float): zenith angle :math:`\\theta` in degrees,
        measured positively from vertical direction
      vetos (dict): different controls, see :mod:`mceq_config`
      obs_ids (list): list of particle name strings. Those lepton decay
        products will be scored in the special ``obs_`` categories
    """
    def __init__(self, interaction_model, atm_model, primary_model,
                 theta_deg, vetos, obs_ids, *args, **kwargs):

        from ParticleDataTool import SibyllParticleTable, PYTHIAParticleData
        from MCEq.data import DecayYields, InteractionYields, HadAirCrossSections

        self.cname = self.__class__.__name__

        # Save atmospheric parameters
        self.atm_config = atm_model
        self.theta_deg = theta_deg

        # Save yields class parameters
        self.yields_params = dict(interaction_model=interaction_model)
        #: handler for decay yield data of type :class:`MCEq.data.InteractionYields`
        self.y = InteractionYields(**self.yields_params)

        # Load decay spectra
        # TODO: weights is temporary argument
        self.ds_params = dict(weights=self.y.weights)
        #: handler for decay yield data of type :class:`MCEq.data.DecayYields`
        self.ds = DecayYields(**self.ds_params)

        # Load cross-section handling
        self.cs_params = dict(interaction_model=interaction_model)
        #: handler for cross-section data of type :class:`MCEq.data.HadAirCrossSections`
        self.cs = HadAirCrossSections(**self.cs_params)

        # Save primary model params
        self.pm_params = primary_model

        #: instance of :class:`ParticleDataTool.PYTHIAParticleData`: access to
        #: properties of particles, like mass and charge
        self.pd = PYTHIAParticleData()

        #: instance of :class:`ParticleDataTool.SibyllParticleTable`: access to
        #: properties lists of particles, index translation etc.
        self.modtab = SibyllParticleTable()

        # Store vetos
        self.vetos = vetos

        # Save observer id
        self.set_obs_particles(obs_ids)

        # General Matrix dimensions and shortcuts, controlled by
        # grid of yield matrices
        #: (int) dimension of energy grid
        self.d = self.y.dim
        #: (np.array) energy grid (bin centers)
        self.e_grid = self.y.e_grid

        # Hadron species include the everything excluding pure resonances
        self.particle_species, self.cascade_particles, self.resonances = \
            self._gen_list_of_particles()

        # Particle index shortcuts
        #: (dict) Converts PDG ID to index in state vector
        self.pdg2nceidx = {}
        #: (dict) Converts particle name to index in state vector
        self.pname2nceidx = {}
        #: (dict) Converts PDG ID to reference of :class:`data.NCEParticle`
        self.pdg2pref = {}
        #: (dict) Converts particle name to reference of :class:`data.NCEParticle`
        self.pname2pref = {}
        #: (dict) Converts index in state vector to PDG ID
        self.nceidx2pdg = {}
        #: (dict) Converts index in state vector to reference of :class:`data.NCEParticle`
        self.nceidx2pname = {}

        for p in self.particle_species:
            try:
                nceidx = p.nceidx
            except:
                nceidx = -1
            self.pdg2nceidx[p.pdgid] = nceidx
            self.pname2nceidx[p.name] = nceidx
            self.nceidx2pdg[nceidx] = p.pdgid
            self.nceidx2pname[nceidx] = p.name
            self.pdg2pref[p.pdgid] = p
            self.pname2pref[p.name] = p

        # Further short-cuts depending on previous initializations
        self.n_tot_species = len(self.cascade_particles)

        self.dim_states = self.d * self.n_tot_species

        self.I = np.eye(self.dim_states)
        self.e_weight = np.array(self.n_tot_species *
                                 list(self.y.e_bins[1:] -
                                      self.y.e_bins[:-1]))

        self._init_alias_tables()

        def print_in_rows(str_list, n_cols=8):
            l = len(str_list)
            n_full_length = int(l / n_cols)
            n_rest = l % n_cols
            print_str = '\n'
            for i in range(n_full_length):
                print_str += ('"{:}", ' * n_cols).format(*str_list[i * n_cols:(i + 1)
                                                         * n_cols]) + '\n'
            print_str += ('"{:}", ' * n_rest).format(*str_list[-n_rest:])

            print print_str.strip()[:-1]


        if dbg > 0:
            print "\nHadrons:\n"
            print_in_rows([p.name for p in self.particle_species if p.is_hadron
                           and not p.is_resonance and not p.is_mixed])

            print "\nMixed:\n"
            print_in_rows(
                [p.name for p in self.particle_species if p.is_mixed])

            print "\nResonances:\n"
            print_in_rows(
                [p.name for p in self.particle_species if p.is_resonance])

            print "\nLeptons:\n"
            print_in_rows([p.name for p in self.particle_species if p.is_lepton
                           and not p.is_alias])
            print "\nAliases:\n",
            print_in_rows(
                [p.name for p in self.particle_species if p.is_alias])

            print "\nTotal number of species:", self.n_tot_species

        # list particle indices
        self.part_str_vec = []
        if dbg > 2:
            print "Particle matrix indices:"
            some_index = 0
            for p in self.cascade_particles:
                for i in xrange(self.d):
                    self.part_str_vec.append(p.name + '_' + str(i))
                    some_index += 1
                    if (dbg):
                        print p.name + '_' + str(i), some_index

        # Set interaction model and compute grids and matrices
        if interaction_model != None:
            self.delay_pmod_init = False
            self.set_interaction_model(interaction_model)
        else:
            self.delay_pmod_init = True

        # Set atmosphere and geometry
        if atm_model != None:
            self.set_atm_model(self.atm_config)

        # Set initial flux condition
        if primary_model != None:
            self.set_primary_model(*self.pm_params)

    def _gen_list_of_particles(self):
        """Determines the list of particles for calculation and
        returns lists of instances of :class:`data.NCEParticle` .

        The particles which enter this list are those, which have a
        defined index in the SIBYLL 2.3 interaction model. Included are
        most relevant baryons and mesons and some of their high mass states.
        More details about the particles which enter the calculation can
        be found in :mod:`ParticleDataTool`.

        Returns:
          (tuple of lists of :class:`data.NCEParticle`): (all particles,
          cascade particles, resonances)
        """
        from MCEq.data import NCEParticle

        if dbg > 1:
            print (self.cname + "::_gen_list_of_particles():" +
                   "Generating particle list.")

        particles = self.modtab.baryons + self.modtab.mesons + self.modtab.leptons
        particle_list = [NCEParticle(h, self.modtab, self.pd,
                                     self.cs, self.d) for h in particles]

        particle_list.sort(key=lambda x: x.E_crit, reverse=False)

        for p in particle_list:
            p.calculate_mixing_energy(self.e_grid,
                                      self.vetos['no_mixing'])

        cascade_particles = [p for p in particle_list if not p.is_resonance]
        resonances = [p for p in particle_list if p.is_resonance]

        for nceidx, h in enumerate(cascade_particles):
            h.nceidx = nceidx

        return cascade_particles + resonances, cascade_particles, resonances

    def _init_alias_tables(self):
        """Sets up the functionality of aliases and defines the meaning of
        'prompt'.

        The identification of the last mother particle of a lepton is implemented
        via index aliases. I.e. the PDG index of muon neutrino 14 is transformed
        into 7114 if it originates from decays of a pion, 7214 in case of kaon or
        7014 if the mother particle is very short lived (prompt). The 'very short lived'
        means that the critical energy :math:`\\varepsilon \\ge \\varepsilon(D^\pm)`.
        This includes all charmed hadrons, as well as resonances such as :math:`\\eta`.

        The aliases for the special ``obs_`` category are also initialized here.
        """
        if dbg > 1:
            print (self.cname + "::_init_alias_tables():" +
                   "Initializing links to alias IDs.")
        self.alias_table = {}
        prompt_ids = []
        for p in self.particle_species:
            if p.is_lepton or p.is_alias or p.pdgid < 0:
                continue
            if p.E_crit >= self.pdg2pref[411].E_crit:
                prompt_ids.append(p.pdgid)
        for lep_id in [12, 13, 14, 16]:
            self.alias_table[(211, lep_id)] = 7100 + lep_id  # pions
            self.alias_table[(321, lep_id)] = 7200 + lep_id  # kaons
            for pr_id in prompt_ids:
                self.alias_table[(pr_id, lep_id)] = 7000 + lep_id  # prompt

        # check if leptons coming from mesons located in obs_ids should be
        # in addition scored in a separate category (73xx)
        self.obs_table = {}
        if self.obs_ids != None:
            for obs_id in self.obs_ids:
                if obs_id in self.pdg2pref.keys():
                    self.obs_table.update({
                        (obs_id, 12): 7312,
                        (obs_id, 13): 7313,
                        (obs_id, 14): 7314,
                        (obs_id, 16): 7316})

    def _init_Lambda_int(self):
        """Initializes the interaction length vector according to the order
        of particles in state vector.

        :math:`\\boldsymbol{\\Lambda_{int}} = (1/\\lambda_{int,0},...,1/\\lambda_{int,N})`
        """
        self.Lambda_int = np.hstack([p.inverse_interaction_length()
                                     for p in self.cascade_particles])

    def _init_Lambda_dec(self):
        """Initializes the decay length vector according to the order
        of particles in state vector. The shortest decay length is determined
        here as well.

        :math:`\\boldsymbol{\\Lambda_{dec}} = (1/\\lambda_{dec,0},...,1/\\lambda_{dec,N})`
        """
        self.Lambda_dec = np.hstack([p.inverse_decay_length(self.e_grid)
                            for p in self.cascade_particles])
        self.max_ldec = np.max(self.Lambda_dec)

    def _convert_to_sparse(self):
        """Converts interaction and decay matrix into sparse format using
        :class:`scipy.sparse.csr_matrix`.
        """

        from scipy.sparse import csr_matrix
        if dbg > 0:
            print (self.cname + "::_convert_to_sparse():" +
                   "Converting to sparse (CSR) matrix format.")
        self.int_m = csr_matrix(self.int_m)
        self.dec_m = csr_matrix(self.dec_m)

    def _init_default_matrices(self):
        """Constructs the matrices for calculation.

        These are:

        - :math:`\\boldsymbol{M}_{int} = (-\\boldsymbol{1} + \\boldsymbol{C}){\\boldsymbol{\\Lambda}}_{int}`,
        - :math:`\\boldsymbol{M}_{dec} = (-\\boldsymbol{1} + \\boldsymbol{D}){\\boldsymbol{\\Lambda}}_{dec}`.

        For ``dbg > 0`` some general information about matrix shape and the number of
        non-zero elements is printed. The intermediate matrices :math:`\\boldsymbol{C}` and
        :math:`\\boldsymbol{D}` are deleted afterwards to save memory.
        """
        print self.cname + "::_init_default_matrices():Start filling matrices."

        self._fill_matrices()

        # interaction part
        self.int_m = (-self.I + self.C) * self.Lambda_int
        # decay part
        self.dec_m = (-self.I + self.D) * self.Lambda_dec

        del self.C, self.D

        if config['use_sparse']:
            self._convert_to_sparse()

        if dbg > 0:
            int_m_density = (float(np.count_nonzero(self.int_m)) /
                         float(self.int_m.size))
            dec_m_density = (float(np.count_nonzero(self.dec_m)) /
                         float(self.dec_m.size))
            print "C Matrix info:"
            print "    density    :", int_m_density
            print "    shape      :", self.int_m.shape
            if config['use_sparse']:
                print "    nnz        :", self.int_m.nnz
            if dbg > 1:
                print "    sum        :", np.sum(self.int_m)
            print "D Matrix info:"
            print "    density    :", dec_m_density
            print "    shape      :", self.dec_m.shape
            if config['use_sparse']:
                print "    nnz        :", self.dec_m.nnz
            if dbg > 1:
                print "    sum        :", np.sum(self.dec_m)


        print self.cname + "::_init_default_matrices():Done filling matrices."

    def _init_progress_bar(self, maximum):
        """Initializes the progress bar.

        The progress bar is a small python package which shows a progress
        bar and remaining time. It should you cost no time to install it
        from your favorite repositories such as pip, easy_install, anaconda, etc.

        Raises:
          ImportError: if package not available
        """
        try:
            from progressbar import ProgressBar, Percentage, Bar, ETA
        except ImportError:
            print "Failed to import 'progressbar' progress indicator."
            print "Install the module with 'easy_install progressbar', or",
            print "get it from http://qubit.ic.unicamp.br/~nilton"
            raise ImportError("It's easy do do this...")
        self.progressBar = ProgressBar(maxval=maximum,
                                       widgets=[Percentage(), ' ',
                                                Bar(), ' ',
                                                ETA()])

    def _alias(self, mother, daughter):
        """Returns pair of alias indices, if ``mother``/``daughter`` combination
        belongs to an alias.

        Args:
          mother (int): PDG ID of mother particle
          daughter (int): PDG ID of daughter particle
        Returns:
          tuple(int, int): lower and upper index in state vector of alias or ``None``
        """


        ref = self.pdg2pref
        abs_mo = np.abs(mother)
        abs_d = np.abs(daughter)
        si_d = np.sign(daughter)
        if (abs_mo, abs_d) in self.alias_table.keys():
            return (ref[si_d * self.alias_table[(abs_mo, abs_d)]].lidx(),
                    ref[si_d * self.alias_table[(abs_mo, abs_d)]].uidx())
        else:
            return None

    def _alternate_score(self, mother, daughter):
        """Returns pair of special score indices, if ``mother``/``daughter`` combination
        belongs to the ``obs_`` category.

        Args:
          mother (int): PDG ID of mother particle
          daughter (int): PDG ID of daughter particle
        Returns:
          tuple(int, int): lower and upper index in state vector of ``obs_`` group or ``None``
        """

        ref = self.pdg2pref
        abs_mo = np.abs(mother)
        abs_d = np.abs(daughter)
        si_d = np.sign(daughter)
        if (abs_mo, abs_d) in self.obs_table.keys():
            return (ref[si_d * self.obs_table[(abs_mo, abs_d)]].lidx(),
                    ref[si_d * self.obs_table[(abs_mo, abs_d)]].uidx())
        else:
            return None

    def get_solution(self, particle_name, mag=0., grid_idx=None):
        """Retrieves solution of the calculation on the energy grid.

        Some special prefixes are accepted for lepton names:

        - the total flux of muons, muon neutrinos etc. from all sources/mothers
          can be retrieved by the prefix ``total_``, i.e. ``total_numu``
        - the conventional flux of muons, muon neutrinos etc. from all sources
          can be retrieved by the prefix ``conv_``, i.e. ``conv_numu``
        - correspondigly, the flux of leptons which originated from the decay
          of a charged pion carries the prefix ``pi_`` and from a kaon ``k_``
        - conventional leptons originating neither from pion nor from kaon
          decay are collected in a category without any prefix, e.g. ``numu`` or
          ``mu+``

        Args:
          particle_name (str): The name of the particle such, e.g.
            ``total_mu+`` for the total flux spectrum of positive muons or
            ``pr_antinumu`` for the flux spectrum of prompt anti muon neutrinos
          mag (float, optional): 'magnification factor': the solution is
            multiplied by ``sol`` :math:`= \\Phi \\cdot E^{mag}`
          grid_idx (int, optional): if the integrator has been configured to save
            intermediate solutions on a depth grid, then ``grid_idx`` specifies
            the index of the depth grid for which the solution is retrieved. If
            not specified the flux at the surface is returned

        Returns:
          (numpy.array): flux of particles on energy grid :attr:`e_grid`
        """
        res = np.zeros(self.d)
        ref = self.pname2pref
        sol = None
        if grid_idx == None:
            sol = self.solution
        else:
            sol = self.grid_sol[grid_idx]

        if particle_name.startswith('total'):
            lep_str = particle_name.split('_')[1]
            for prefix in ('pr_', 'pi_', 'k_', ''):
                particle_name = prefix + lep_str
                res += sol[ref[particle_name].lidx():
                           ref[particle_name].uidx()] * \
                    self.e_grid ** mag
        elif particle_name.startswith('conv'):
            lep_str = particle_name.split('_')[1]
            for prefix in ('pi_', 'k_', ''):
                particle_name = prefix + lep_str
                res += sol[ref[particle_name].lidx():
                           ref[particle_name].uidx()] * \
                    self.e_grid ** mag
        else:
            res = sol[ref[particle_name].lidx():
                      ref[particle_name].uidx()] * \
                self.e_grid ** mag
        return res

    def set_obs_particles(self, obs_ids):
        """Adds a list of mother particle strings which decay products
        should be scored in the special ``obs_`` category.

        Decay and interaction matrix will be regenerated automatically
        after performing this call.

        Args:
          obs_ids (list of strings): mother particle names
        """
        if obs_ids == None:
            self.obs_ids = None
            return
        self.obs_ids = []
        for obs_id in obs_ids:
            try:
                self.obs_ids.append(int(obs_id))
            except ValueError:
                self.obs_ids.append(self.modtab.modname2pdg[obs_id])
        if dbg:
            print 'MCEqRun::set_obs_particles(): Converted names:' + \
                ', '.join([str(oid) for oid in obs_ids]) + \
                '\nto: ' + ', '.join([str(oid) for oid in self.obs_ids])

        self._init_alias_tables()
        self._init_default_matrices()

    def set_interaction_model(self, interaction_model, charm_model=None):
        """Sets interaction model and/or an external charm model for calculation.

        Decay and interaction matrix will be regenerated automatically
        after performing this call.

        Args:
          interaction_model (str): name of interaction model
          charm_model (str, optional): name of charm model
        """
        if dbg:
            print 'MCEqRun::set_interaction_model(): ', interaction_model

        self.yields_params['interaction_model'] = interaction_model
        self.yields_params['charm_model'] = charm_model

        #If a custom charm model is selected force re-read of yields
        self.y.set_interaction_model(interaction_model)
        self.y.inject_custom_charm_model(charm_model)

        self.cs_params['interaction_model'] = interaction_model
        self.cs.set_interaction_model(interaction_model)

        # Initialize default run
        self._init_Lambda_int()
        self._init_Lambda_dec()

        for p in self.particle_species:
            if p.pdgid in self.y.projectiles:
                p.is_projectile = True
                p.secondaries = \
                    self.y.secondary_dict[p.pdgid]
            elif p.pdgid in self.ds.daughter_dict:
                p.daughters = self.ds.daughters(p.pdgid)
                p.is_projectile = False
            else:
                p.is_projectile = False

        # initialize matrices
        self._init_default_matrices()

        self.iamodel_name = interaction_model

        if self.delay_pmod_init:
            self.delay_pmod_init = False
            self.set_primary_model(*self.pm_params)

    def set_primary_model(self, mclass, tag):
        """Sets primary flux model.

        This functions is quick and does not require re-generation of
        matrices.

        Args:
          interaction_model (:class:`CRFluxModel.PrimaryFlux`): reference
          to primary model **class**
          tag (tuple): positional argument list for model class
        """
        if self.delay_pmod_init:
            if dbg > 1:
                print 'MCEqRun::set_primary_model(): Initialization delayed..'
            return
        if dbg > 0:
            print 'MCEqRun::set_primary_model(): ', mclass.__name__, tag

        # Initialize primary model object
        self.pmodel = mclass(tag)
        self.get_nucleon_spectrum = np.vectorize(self.pmodel.p_and_n_flux)

        try:
            self.dim_states
        except:
            self.finalize_pmodel = True

        # Initial condition
        phi0 = np.zeros(self.dim_states)
        p_top, n_top = self.get_nucleon_spectrum(self.e_grid)[1:]
        phi0[self.pdg2pref[2212].lidx():
             self.pdg2pref[2212].uidx()] = 1e-4 * p_top
        phi0[self.pdg2pref[2112].lidx():
             self.pdg2pref[2112].uidx()] = 1e-4 * n_top

        # Save initial condition
        self.phi0 = phi0

    def set_single_primary_particle(self, E, corsika_id):
        """Set type and energy of a single primary nucleus to
        calculation of particle yields.

        The functions uses the superposition theorem, where the flux of
        a nucleus with mass A and charge Z is modeled by using Z protons
        and A-Z neutrons at energy :math:`E_{nucleon}= E_{nucleus} / A`
        The nucleus type is defined via :math:`\\text{CORSIKA ID} = A*100 + Z`. For
        example iron has the CORSIKA ID 5226.

        A continuous input energy range is allowed between
        :math:`50*A\ \\text{GeV} < E_\\text{nucleus} < 10^{10}*A\ \\text{GeV}`.

        Args:
          E (float): (total) energy of nucleus in GeV
          corsika_id (int): ID of nucleus (see text)
        """
        if self.delay_pmod_init:
            if dbg > 1:
                print 'MCEqRun::set_single_primary_particle(): Initialization delayed..'
            return
        if dbg > 0:
            print ('MCEqRun::set_single_primary_particle(): corsika_id={0}, ' +
                   'particle energy={1:5.3g} GeV').format(corsika_id, E)

        try:
            self.dim_states
        except:
            self.finalize_pmodel = True

        E_gr = self.e_grid
        widths = self.y.e_bins[1:] - self.y.e_bins[:-1]

        w_scale = widths[0] / E_gr[0]

        n_protons = 0
        n_neutrons = 0

        if corsika_id == 14:
            n_protons = 1
        else:
            Z = corsika_id % 100
            A = (corsika_id - Z) / 100
            n_protons = Z
            n_neutrons = A - Z
            # convert energy to energy per nucleon
            E = E / float(A)

        if dbg > 1:
            print ('MCEqRun::set_single_primary_particle(): superposition:' +
                   'n_protons={0}, n_neutrons={1}, ' +
                   'energy per nucleon={2:5.3g} GeV').format(n_protons, n_neutrons, E)


        # find energy grid index closest to E
        idx_min = np.argmin(abs(E - E_gr))
        idx_up, idx_lo = 0, 0

        if E_gr[idx_min] < E:
            idx_up = idx_min + 1
            idx_lo = idx_min
        else:
            idx_up = idx_min
            idx_lo = idx_min - 1
        # calculate the effective bin width at E
        wE = E * w_scale
        E_up = E + wE / 2.
        E_lo = E - wE / 2.
        # determine partial widths in 2 neighboring bins
        wE_up = E_up - (E_gr[idx_up] - widths[idx_up] / 2.)
        wE_lo = E_gr[idx_lo] + widths[idx_lo] / 2. - E_lo

        self.phi0 = np.zeros(self.dim_states)

        if dbg > 1:
            print ('MCEqRun::set_single_primary_particle(): \n \t' +
                   'fractional contribution for lower bin @ E={0:5.3g} GeV: {1:5.3} \n \t' +
                   'fractional contribution for upper bin @ E={2:5.3g} GeV: {3:5.3}').format(
                                                     E_gr[idx_lo], wE_lo / widths[idx_lo],
                                                     E_gr[idx_up], wE_up / widths[idx_up])

        self.phi0[self.pdg2pref[2212].lidx() + idx_lo] = n_protons * wE_lo / widths[idx_lo] ** 2
        self.phi0[self.pdg2pref[2212].lidx() + idx_up] = n_protons * wE_up / widths[idx_up] ** 2


        self.phi0[self.pdg2pref[2112].lidx() + idx_lo] = n_neutrons * wE_lo / widths[idx_lo] ** 2
        self.phi0[self.pdg2pref[2112].lidx() + idx_up] = n_neutrons * wE_up / widths[idx_up] ** 2

    def set_atm_model(self, atm_config):
        """Sets model of the atmosphere.

        To choose, for example, a CORSIKA parametrization for the Southpole in January,
        do the following::

            mceq_instance.set_atm_model(('CORSIKA', 'PL_SouthPole', 'January'))

        More details about the choices can be found in :mod:`MCEq.density_profiles`. Calling
        this method will issue a recalculation of the interpolation and the integration path.

        Args:
          atm_config (tuple of strings): (parametrization type, location string, season string)
        """
        from MCEq.density_profiles import CorsikaAtmosphere, MSIS00Atmosphere

        base_model, location, season = atm_config

        if dbg:
            print 'MCEqRun::set_atm_model(): ', base_model, location, season

        if base_model == 'MSIS00':
            self.atm_model = MSIS00Atmosphere(
                location, season)
        elif base_model == 'CORSIKA':
            self.atm_model = CorsikaAtmosphere(
                location, season)
        else:
            raise Exception(
                'MCEqRun::set_atm_model(): Unknown atmospheric base model.')
        self.atm_config = atm_config

        if self.theta_deg != None:
            self.set_theta_deg(self.theta_deg)

    def set_theta_deg(self, theta_deg):
        """Sets zenith angle :math:`\\theta` as seen from a detector.

        Currently only 'down-going' angles (0-90 degrees) are supported.

        Args:
          atm_config (tuple of strings): (parametrization type, location string, season string)
        """
        if dbg:
            print 'MCEqRun::set_theta_deg(): ', theta_deg

        if self.atm_config == None or not bool(self.atm_model):
            raise Exception(
                'MCEqRun::set_theta_deg(): Can not set theta, since ' +
                'atmospheric model not properly initialized.')

        if self.atm_model.theta_deg == theta_deg:
            print 'Theta selection correponds to cached value, skipping calc.'
            return

        self.atm_model.set_theta(theta_deg)
        self.integration_path = None

    def _zero_mat(self):
        return np.zeros((self.d, self.d))

    def _follow_chains(self, p, pprod_mat, p_orig, idcs,
                      propmat, reclev=0):
        r = self.pdg2pref

        if dbg > 2:
            print reclev * '\t', 'entering with', r[p].name

        for d in self.ds.daughters(p):
            if dbg > 2:
                print reclev * '\t', 'following to', r[d].name

            dprop = self._zero_mat()
            self.ds.assign_d_idx(r[p].pdgid, idcs,
                                 r[d].pdgid, r[d].hadridx(),
                                 dprop)
            alias = self._alias(p, d)

            # Check if combination of mother and daughter has a special alias
            # assigned and the index has not be replaced (i.e. pi, K, prompt)
            if not alias:
                propmat[r[d].lidx():r[d].uidx(),
                        r[p_orig].lidx():r[p_orig].uidx()] += dprop.dot(pprod_mat)
            else:
                propmat[alias[0]:alias[1],
                        r[p_orig].lidx():r[p_orig].uidx()] += dprop.dot(pprod_mat)

            alt_score = self._alternate_score(p, d)
            if alt_score:
                propmat[alt_score[0]:alt_score[1],
                        r[p_orig].lidx():r[p_orig].uidx()] += dprop.dot(pprod_mat)

            if dbg > 2:
                pstr = 'res'
                dstr = 'Mchain'
                if idcs == r[p].hadridx():
                    pstr = 'prop'
                    dstr = 'Mprop'
                print (reclev * '\t',
                       'setting {0}[({1},{3})->({2},{4})]'.format(
                           dstr, r[p_orig].name, r[d].name, pstr, 'prop'))
                print r[p].name

            if r[d].is_mixed:
                dres = self._zero_mat()
                self.ds.assign_d_idx(r[p].pdgid, idcs,
                                     r[d].pdgid, r[d].residx(),
                                     dres)
                reclev += 1
                self._follow_chains(d, dres.dot(pprod_mat),
                                    p_orig, r[d].residx(), propmat, reclev)
            else:
                if dbg > 2:
                    print reclev * '\t', '\t terminating at', r[d].name

    def _fill_matrices(self):
        # Initialize empty matrices
        self.C = np.zeros((self.dim_states, self.dim_states))
        self.D = np.zeros((self.dim_states, self.dim_states))

        # self.R = self.get_empty_matrix() # R matrix is obsolete

        pref = self.pdg2pref

        for p in self.cascade_particles:
            # Fill parts of the D matrix related to p as mother
            if self.ds.daughters(p.pdgid):
                self._follow_chains(p.pdgid, np.diag(np.ones((self.d))),
                                    p.pdgid, p.hadridx(),
                                    self.D, reclev=0)

            # if p doesn't interact, skip interaction matrices
            if not p.is_projectile:
                continue

            # go through all secondaries
            for s in p.secondaries:
                if not pref[s].is_resonance:
                    cmat = self._zero_mat()
                    self.y.assign_yield_idx(p.pdgid,
                                            p.hadridx(),
                                            pref[s].pdgid,
                                            pref[s].hadridx(),
                                            cmat)
                    self.C[pref[s].lidx():pref[s].uidx(),
                                 p.lidx():p.uidx()] += cmat

                cmat = self._zero_mat()
                self.y.assign_yield_idx(p.pdgid,
                                        p.hadridx(),
                                        pref[s].pdgid,
                                        pref[s].residx(),
                                        cmat)
                self._follow_chains(pref[s].pdgid, cmat,
                                    p.pdgid, pref[s].residx(),
                                    self.C, reclev=1)

    def solve(self, **kwargs):

        if dbg > 1:
            print (self.cname + "::solve(): " +
                   "solver={0} and sparse={1}").format(self.solver,
                                                       self.sparse)

        if config['integrator'] != "odepack":
            self._forward_euler(**kwargs)
        elif config['integrator'] == 'odepack':
            self._odepack(**kwargs)
        else:
            raise Exception(
                ("MCEq::solve(): Unknown integrator selection '{0}'."
                 ).format(config['integrator']))

    def _odepack(self, dXstep=1., initial_depth=0.1,
                 *args, **kwargs):
        from scipy.integrate import ode
        ri = self.atm_model.r_X2rho

        # Functional to solve
        def dPhi_dX(X, phi, *args):
            return self.int_m.dot(phi) + self.dec_m.dot(ri(X) * phi)

        # Jacobian doesn't work with sparse matrices, and any precision
        # or speed advantage disappear if used with dense algebra
        def jac(X, phi, *args):
            print 'jac', X, phi
            return (self.int_m + self.dec_m * ri(X)).todense()

        # Initial condition
        phi0 = np.copy(self.phi0)

        # Setup solver
        r = ode(dPhi_dX).set_integrator(
            with_jacobian=False, **config['ode_params'])

        r.set_initial_value(phi0, initial_depth)

        # Solve
        X_surf = self.atm_model.X_surf

        self._init_progress_bar(X_surf)
        self.progressBar.start()
        start = time()

        while r.successful() and r.t < X_surf:
            self.progressBar.update(r.t)
#             if (i % 100) == 0:
#                 print "Solving at depth X =", r.t, X_i
            r.integrate(r.t + dXstep)

        # Do last step to make sure the rational number X_surf is reached
        r.integrate(X_surf)

        self.progressBar.finish()

        print ("\n{0}::vode(): time elapsed during " +
               "integration: {1} sec").format(self.cname, time() - start)

        self.solution = r.y

    def _forward_euler(self, int_grid=None, grid_var='X'):

        # Calculate integration path if not yet happened
        self._calculate_integration_path(int_grid, grid_var)

        phi0 = np.copy(self.phi0)
        nsteps, dX, rho_inv, grid_idcs = self.integration_path

        if dbg > 0:
            print ("{0}::_forward_euler(): Solver will perform {1} " +
                   "integration steps.").format(self.cname, nsteps)

        self._init_progress_bar(nsteps)
        self.progressBar.start()

        start = time()

        import kernels
        if config['kernel_config'] == 'numpy':
            kernel = kernels.kern_numpy

        elif (config['kernel_config'] == 'CUDA' and
              config['use_sparse'] == False):
            kernel = kernels.kern_CUDA_dense

        elif (config['kernel_config'] == 'CUDA' and
              config['use_sparse'] == True):
            kernel = kernels.kern_CUDA_sparse

        elif (config['kernel_config'] == 'MKL' and
              config['use_sparse'] == True):
            kernel = kernels.kern_MKL_sparse
        else:
            raise Exception(
                ("MCEq::_forward_euler(): " +
                "Unsupported integrator settings '{0}/{1}'."
                 ).format(
                'sparse' if config['use_sparse'] else 'dense',
                config['kernel_config']))


        self.solution, self.grid_sol = kernel(nsteps, dX, rho_inv,
            self.int_m, self.dec_m, phi0, grid_idcs, self.progressBar)

        self.progressBar.finish()

        print ("\n{0}::_forward_euler(): time elapsed during " +
               "integration: {1} sec").format(self.cname, time() - start)

    def _calculate_integration_path(self, int_grid, grid_var):

        print "MCEqRun::_calculate_integration_path():"

        if (self.integration_path and np.alltrue(int_grid == self.int_grid) and
            np.alltrue(self.grid_var == grid_var)):
            return

        self.int_grid, self.grid_var = int_grid, grid_var
        if grid_var != 'X':
            raise NotImplementedError('MCEqRun::_calculate_integration_path():' +
               'choice of grid variable other than the depth X are not possible, yet.')

        X_surf = self.atm_model.X_surf
        ri = self.atm_model.r_X2rho
        max_ldec = self.max_ldec

        dX_vec = []
        rho_inv_vec = []

        X = 0.
        step = 0
        grid_step = 0
        grid_idcs = []

        self._init_progress_bar(X_surf)
        self.progressBar.start()

        while X < X_surf:
            self.progressBar.update(X)
            ri_x = ri(X)
            dX = 1. / (max_ldec * ri_x)
            if (np.any(int_grid) and (grid_step < int_grid.size)
                and (X + dX >= int_grid[grid_step])):
                dX = int_grid[grid_step] - X
                grid_idcs.append(step)
                grid_step += 1
            dX_vec.append(dX)
            rho_inv_vec.append(ri_x)
            X = X + dX
            step += 1

        # Integrate
        self.progressBar.finish()

        dX_vec = np.array(dX_vec, dtype=np.float32)
        rho_inv_vec = np.array(rho_inv_vec, dtype=np.float32)
        self.integration_path = dX_vec.size, dX_vec, \
                                rho_inv_vec, grid_idcs

class EdepZFactors():

    def __init__(self, interaction_model,
                 primary_flux_model):
        from MCEq.data import InteractionYields, HadAirCrossSections
        from ParticleDataTool import SibyllParticleTable
        from misc import get_bins_and_width_from_centers

        self.y = InteractionYields(interaction_model)
        self.cs = HadAirCrossSections(interaction_model)

        self.pm = primary_flux_model
        self.e_bins, self.e_widths = get_bins_and_width_from_centers(
            self.y.e_grid)
        self.e_vec = self.y.e_grid
        self.iamod = interaction_model
        self.sibtab = SibyllParticleTable()
        self._gen_integrator()

    def get_zfactor(self, proj, sec_hadr, logx=False, use_cs=True):
        proj_cs_vec = self.cs.get_cs(proj)
        nuc_flux = self.pm.tot_nucleon_flux(self.e_vec)
        zfac = np.zeros(self.y.dim)
        sec_hadr = sec_hadr
        if self.y.is_yield(proj, sec_hadr):
            if dbg > 1:
                print (("EdepZFactors::get_zfactor(): " +
                        "calculating zfactor Z({0},{1})").format())
            y_mat = self.y.get_y_matrix(proj, sec_hadr)

            self.calculate_zfac(self.e_vec, self.e_widths,
                                nuc_flux, proj_cs_vec,
                                y_mat, zfac, use_cs)

        if logx:
            return np.log10(self.e_vec), zfac
        return self.e_vec, zfac

    def _gen_integrator(self):
        try:
            from numba import jit, double
            @jit(argtypes=[double[:], double[:], double[:],
                           double[:, :], double[:]], target='cpu')
            def calculate_zfac(e_vec, e_widths, nuc_flux, proj_cs, y, zfac, use_cs):
                for h, E_h in enumerate(e_vec):
                    for k in range(len(e_vec)):
                        E_k = e_vec[k]
                        dE_k = e_widths[k]
                        if E_k < E_h:
                            continue
                        csfac = proj_cs[k] / proj_cs[h] if use_cs else 1.

                        zfac[h] += nuc_flux[k] / nuc_flux[h] * csfac * \
                            y[:, k][h] * dE_k
        except ImportError:
            print "Warning! Numba not in PYTHONPATH. ZFactor calculation won't work."

        self.calculate_zfac = calculate_zfac
