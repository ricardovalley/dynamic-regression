#%%
import numpy as np
import sys 
from sympy import symbols, simplify, derive_by_array
from scipy.integrate import solve_ivp
from xLSINDy import *
from sympy.physics.mechanics import *
from sympy import *
from sympy.solvers import solve
import sympy
import torch
import math
from math import pi
sys.path.append(r'../../../HLsearch/')
import HLsearch as HL
import time
from itertools import chain
import pickle
import matplotlib.pyplot as plt
from diffrax import diffeqsolve, Dopri5, Euler, ODETerm, SaveAt, Tsit5, PIDController
from jax import numpy as jnp
from jax import config
from jax import lax
config.update("jax_enable_x64", True)

# rootdir = "./Soft Robot/ns-1_dof-1_zero_actuation/"
# rootdir = "./Source/Soft Robot/ns-1_dof-1_tau_act/"
# rootdir = "./Source/Soft Robot/ns-1_bending_tau_act/"
# rootdir = "./Source/Soft Robot/ns-1_bending_zero_damping/"
rootdir = "./Source/Soft Robot/ns-1_elongation_random_actuation/"
# rootdir = "./Source/Soft Robot/check_if_error/"
noiselevel = 0

# Load dataset
X_all = np.load(rootdir + "X.npy")
Xdot_all = np.load(rootdir + "Xdot.npy")
Tau_all = np.load(rootdir + "Tau.npy")

# Select duration of dataset
X_list, Xdot_list, Tau_list = [], [], []
# X_val, Xdot_val, Tau_val = []
for i in range(len(X_all)):
    X_list.append(X_all[i][:5000,:])
    # X_val.append(X_all[i][400:,:])

    Xdot_list.append(Xdot_all[i][:5000,:])
    # Xdot_val.append(Xdot_all[i][400:,:])

    Tau_list.append(Tau_all[i][:5000])
    # Tau_val.append(Tau_all[i][400:])

time = np.arange(0.0, 0.5, 1e-4)
fig, ax = plt.subplots(3,1)
ax[0].plot(time, X_list[0][:,0])
ax[0].grid(True)
ax[1].plot(time, X_list[0][:,1])
ax[1].grid(True)
ax[2].plot(time, Xdot_list[0][:,1])
ax[2].grid(True)
plt.show()

# Stack variables (from all initial conditions)
X = np.vstack(X_list)
Xdot = np.vstack(Xdot_list)
Tau = np.vstack(Tau_list)

# variables without noise (wn)
# X = np.vstack(X)
# Xdot = np.vstack(Xdot)
# X_wn = np.copy(X)
# Xdot_wn = np.copy(Xdot)

#adding noise
# mu, sigma = 0, noiselevel
# noise = np.random.normal(mu, sigma, X_wn.shape[0])
# for i in range(X_wn.shape[1]):
#     X[:,i] = X[:,i] + noise
#     Xdot[:,i] = Xdot_wn[:,i] + noise

# Create the states nomenclature
states_dim = 2
states = ()
states_dot = ()
for i in range(states_dim):
    if(i<states_dim//2):
        states = states + (symbols('x{}'.format(i)),)
        states_dot = states_dot + (symbols('x{}_t'.format(i)),)
    else:
        states = states + (symbols('x{}_t'.format(i-states_dim//2)),)
        states_dot = states_dot + (symbols('x{}_tt'.format(i-states_dim//2)),)
print('states are:',states)
print('states derivatives are: ', states_dot)

states_epsed =()
for i in range(len(states)//2):
    states_epsed = states_epsed + (symbols('x{}_epsed'.format(i)),)

#Turn from sympy to str
states_sym = states
states_dot_sym = states_dot
states_epsed_sym = states_epsed
states = list(str(descr) for descr in states)
states_dot = list(str(descr) for descr in states_dot)
states_epsed = list(str(descr) for descr in states_epsed)

#build function expression for the library in str
expr= HL.buildFunctionExpressions(2,states_dim,states,use_sine=False)
expr.pop(3)
expr.pop(1)
expr = ['x0_t**2', 'x0', 'x0**2']

# expr = HL.buildLagrangianExpressions(5, states_dim, states, use_sine=True)
# expr = ['sin(0.1*x0)*x0_t**2/x0**5', 'x0*x0_t**2/x0**5+x0*cos(0.1*x0)*x0_t**2/x0**5', 'x0**3*x0_t**2/x0**5', 'x0**5*x0_t**2/x0**5', '1/x0**2', 'cos(0.1*x0)/x0**2', 'x0**2']

q_epsed = X[:,0]

# compute time-series tensors for the lagrangian equation
device = 'cuda:0'
Zeta, Eta, Delta = LagrangianLibraryTensor(X, Xdot, expr, states, states_dot, scaling=False, x_epsed=q_epsed, states_epsed=states_epsed)
# Zeta, Eta, Delta = LagrangianLibraryTensor(X,Xdot,expr,states,states_dot,scaling=False)
Eta = Eta.to(device)
Zeta = Zeta.to(device)
Delta = Delta.to(device)

class xLSINDY(torch.nn.Module):
    def __init__(self, xi_L, D):
        
        super().__init__()
        self.coef = torch.nn.Parameter(xi_L)
        # self.register_buffer('weight_update_mask', )
        self.D = torch.nn.Parameter(D) # learn the damping constant
        # self.D = D

    def forward(self, zeta, eta, delta, x_t, device):#, tau):

        tau_pred = ELforward(self.coef, zeta, eta, delta, x_t, device, self.D)
        # q_tt_pred = lagrangianforward(self.coef, zeta, eta, delta, x_t, device, tau)
        return tau_pred

def loss(pred, targ):
    loss = torch.mean((targ - pred)**2) 
    return loss 

def train(model, epochs, lr, lam, bs, optimizer, Zeta, Eta, Delta, Xdot, Tau, stage, scheduler=None):

    if(torch.is_tensor(Tau)==False):
        tau = torch.from_numpy(Tau).to(device).float()

    if (torch.is_tensor(Xdot) == False):
        Xdot = torch.from_numpy(Xdot).to(device).float()

    j = 1
    lossitem_prev = 1e8
    loss_streak = 0
    loss_plot = []
    while (j <= epochs):
        print("\n")
        print("Stage " + str(stage))
        print("Epoch " + str(j) + "/" + str(epochs))
        print("Learning rate : ", lr)

        tl = Xdot.shape[0]
        loss_list = []
        for i in range(tl//bs):
            
            zeta = Zeta[:,:,:,i*bs:(i+1)*bs]
            eta = Eta[:,:,:,i*bs:(i+1)*bs]
            delta = Delta[:,:,i*bs:(i+1)*bs]
            x_t = Xdot[i*bs:(i+1)*bs,:]
            n = x_t.shape[1]

            # If loss using torque
            tau_pred = model.forward(zeta, eta, delta, x_t, device)
            tau_true = (tau[i*bs:(i+1)*bs,:].T).to(device)
            mse_loss = loss(tau_pred, tau_true)

            # If loss using acceleration
            # tau_true = (tau[i*bs:(i+1)*bs,:].T).to(device)
            # q_tt_pred = model.forward(zeta, eta, delta, x_t, device, tau_true)
            # q_tt_true = Xdot[i*bs:(i+1)*bs,n//2:].T
            # mse_loss = loss(q_tt_pred, q_tt_true)

            l1_norm = sum(
                p.abs().sum() for p in model.parameters()
            )
            lossval = mse_loss + lam * l1_norm

            optimizer.zero_grad()
            lossval.backward()
            # model.coef.grad[:5] = 0
            optimizer.step()

            loss_list.append(lossval.item())
        
        lossitem = torch.tensor(loss_list).mean().item()
        print("Average loss : " , lossitem)
        loss_plot.append(lossitem)

        scheduler.step()
        lr = scheduler.get_last_lr()
        # Tolerance (sufficiently good loss)
        if (lossitem <= 1e-8):
            break

        # Early stopping
        # if (lossitem - lossitem_prev) > -1e-4:
        #     loss_streak += 1
        # if loss_streak == 5:
        #     break
        # lossitem_prev = lossitem

        j += 1
    
    return model, loss_plot


## First stage ##

# Initialize coefficients and model
torch.manual_seed(1)
xi_L = torch.ones(len(expr), dtype = torch.float64, device=device).data.uniform_(-0.1,0.1)
xi_L[-1] = -abs(xi_L[-1]) # elastic potential coefficient must be negative in the L expression

D = torch.diag(torch.ones(states_dim//2).data.uniform_(0,0.1)).to(device)
# D = torch.diag(5e-3*torch.ones(states_dim//2)).to(device)
# xi_L = torch.tensor((
#     1/2*(2*pi*(2e-2)**2*1070*(1e-1*1e-1 - math.sin(1e-1*1e-1)))/(1e-1)**3, 
#     pi*(2e-2)**2*1070*(9.81)*(1-math.cos(1e-1*1e-1))/(1e-1)**2,
#     -1/2*(0.12566371),
# ), dtype = torch.float64, device=device)

model = xLSINDY(xi_L, D)

# Training parameters
Epoch = 300
lr = 1e-3 # alpha=learning_rate
lam = 0 # sparsity promoting parameter (l1 regularization)
bs = 2020 # batch size
optimizer = torch.optim.Adam(
    model.parameters(), lr=lr
)
scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[100], gamma=1)

model, loss_plot = train(model, Epoch, lr, lam, bs, optimizer, Zeta, Eta, Delta, Xdot, Tau, 1, scheduler=scheduler)

## Thresholding small indices ##
xi_L = model.coef
D = model.D
threshold = 1e-8
surv_index = ((torch.abs(xi_L) >= threshold)).nonzero(as_tuple=True)[0].detach().cpu().numpy()
# surv_index = np.append(surv_index, xi_L.shape[0]-1)
expr = np.array(expr)[surv_index].tolist()
xi_L = xi_L[surv_index].clone().detach().requires_grad_(True)

## Obtaining analytical model ##
# xi_Lcpu = np.around(xi_L.detach().cpu().numpy(),decimals=10)
xi_Lcpu = xi_L.detach().cpu().numpy()
L = HL.generateExpression(xi_Lcpu,expr,threshold=1e-10)
print(simplify(L))

tau_pred = ELforward(xi_L, Zeta[:,:,:,:5000], Eta[:,:,:,:5000], Delta[:,:,:5000], Xdot[:5000,:], device, D).detach().cpu()
time = np.arange(0.0, 0.5, 1e-4)
plt.plot(time, tau_pred[0,:], '--r', time, Tau[:5000,0])
plt.show()
loss_tau = loss(tau_pred, torch.from_numpy(Tau[:5000,0]))

print('\n')
print('------------')
print('\n')
print('L=' + str(L))
print('D=' + str(D[0,0].detach().cpu()))
print('\n')
print('------------')

# --------------- Validation plots --------------------

# obtain equations of motion
def replaceEpsedStates(states, states_epsed, expr):
    n = len(states)
    #define symbolically
    q = sympy.Array(np.array(sympy.Matrix(states[:n//2])).squeeze().tolist())
    q_epsed = sympy.Array(np.array(sympy.Matrix(states_epsed)).squeeze().tolist())
    qdot = sympy.Array(np.array(sympy.Matrix(states[n//2:])).squeeze().tolist())
    phi = sympy.Array(np.array(sympy.Matrix(expr)).squeeze().tolist())
    phi_q = derive_by_array(phi, q).reshape(n//2, len(phi)) #Delta
    phi_qdot = derive_by_array(phi, qdot).reshape(n//2, len(phi))
    phi_qdot2 = derive_by_array(phi_qdot, qdot).reshape(n//2, n//2, len(phi)) #Zeta
    phi_qdotq = derive_by_array(phi_qdot, q).reshape(n//2, n//2, len(phi)) #Eta

    phi_q_string = np.empty(phi_q.shape, dtype=object)
    for i in range(phi_q.shape[0]):
        for j in range(phi_q.shape[1]):
            if j<phi_q.shape[1]-1:
                phi_q_string[i,j] = str(phi_q[i,j]).replace('x0','x0_epsed')
                phi_q_string[i,j] = phi_q_string[i,j].replace('x0_epsed_t','x0_t')
            else:
                phi_q_string[i,j] = str(phi_q[i,j])
    phi_q = phi_q_string

    phi_qdot2_string = np.empty(phi_qdot2.shape, dtype=object)
    phi_qdotq_string = np.empty(phi_qdot2.shape, dtype=object)
    for i in range(phi_qdot2.shape[0]):
        for j in range(phi_qdot2.shape[1]):
            for k in range(phi_qdot2.shape[2]):
                if k<phi_qdot2.shape[2]-1:
                    phi_qdot2_string[i,j,k] = str(phi_qdot2[i,j,k]).replace('x0','x0_epsed')
                    phi_qdot2_string[i,j,k] = phi_qdot2_string[i,j,k].replace('x0_epsed_t','x0_t')
                    phi_qdotq_string[i,j,k] = str(phi_qdotq[i,j,k]).replace('x0','x0_epsed')
                    phi_qdotq_string[i,j,k] = phi_qdotq_string[i,j,k].replace('x0_epsed_t','x0_t')
                else:
                    phi_qdot2_string[i,j,k] = str(phi_qdot2[i,j,k])
                    phi_qdotq_string[i,j,k] = str(phi_qdotq[i,j,k])
    phi_qdot2 = phi_qdot2_string
    phi_qdotq = phi_qdotq_string

    return phi_q, phi_qdot2, phi_qdotq

def getEOM(D, states, states_dot, states_epsed, states_sym, states_dot_sym, states_epsed_sym, xi_L, expr):
    phi_q, phi_qdot2, phi_qdotq = replaceEpsedStates(states, states_epsed, expr)

    delta_expr = HL.generateExpression(xi_Lcpu,'('+phi_q[0,:]+')',threshold=1e-10)
    eta_expr = HL.generateExpression(xi_Lcpu,'('+phi_qdotq[0,0,:]+')',threshold=1e-10)
    zeta_expr = HL.generateExpression(xi_Lcpu,'('+phi_qdot2[0,0,:]+')',threshold=1e-10)

    # eq_1 = '('+str(zeta_expr)+')'+'*'+states_dot[1]
    # eq_2 = '+('+str(eta_expr)+')'+'*'+states_dot[0]
    # eq_3 = '-'+'('+str(delta_expr)+')'
    # eq_string = eq_1 + eq_2 + eq_3

    # x0 = states_sym[0]
    # x0_t = states_sym[1]
    # x0_tt = states_dot_sym[1]
    # x0_epsed = states_epsed_sym[0]

    # eq = eval(eq_string)
    # rhs = solve(eq, x0_tt)^

    # return rhs[0]
    return delta_expr, eta_expr, zeta_expr

delta_expr, eta_expr, zeta_expr = getEOM(D, states, states_dot, states_epsed, states_sym, states_dot_sym, states_epsed_sym, xi_Lcpu, expr)

delta_expr_lambda = sympy.lambdify((states_sym[0], states_sym[1], states_epsed_sym[0]), delta_expr, 'jax')
eta_expr_lambda = sympy.lambdify((states_sym[1], states_epsed_sym[0]), eta_expr, 'jax')
zeta_expr_lambda = sympy.lambdify((states_epsed_sym[0]), zeta_expr, 'jax')

def generate_data(func, time, init_values, Tau):
    # sol = solve_ivp(func,[time[0],time[-1]],init_values,t_eval=time,method='RK45',rtol=1e-10,atol=1e-10)
    dt = time[1]-time[0]
    sol_list = []
    sol_list.append(init_values)

    indexes = np.unique(Tau, return_index=True)[1]
    tau_unique = [Tau[index] for index in sorted(indexes)]

    for count, t in enumerate(time[::100]):
        # tau = Tau[int(t/dt),0]
        tau = tau_unique[count][0]
        
        if t==0:
            sol = diffeqsolve(
                ODETerm(func),
                solver=Tsit5(),
                t0=time[0],
                t1=time[::100][1],
                dt0=dt,
                y0=init_values,
                args=(tau, D[0,0].detach().cpu().numpy()),
                max_steps=None,
                saveat=SaveAt(ts=jnp.arange(0.0, time[::100][1]+dt, dt)),
            )
        else:
            sol = diffeqsolve(
                ODETerm(func),
                solver=Tsit5(),
                t0=time[0],
                t1=time[::100][1],
                dt0=dt,
                y0=sol_list[-1][-1],
                args=(tau, D[0,0].detach().cpu().numpy()),
                max_steps=None,
                saveat=SaveAt(ts=jnp.arange(0.0, time[::100][1]+dt, dt)),
            )
        
        sol_list.append(sol.ys[1:,:])
    
    sol_array = np.vstack(sol_list)
    sol_array = sol_array[:-1,:]
            
    return sol_array, np.array([func(time[i],sol_array[i,:],(Tau[i,0], D[0,0].detach().cpu().numpy())) for i in range(sol_array.shape[0])],dtype=np.float64)
    # first output is X array in [x,x_dot] format
    # second output is X_dot array in [x_dot,x_doubledot] format

def softrobot(t,x,args):
    x0 = x[0]
    x0_epsed = x[0]
    x0_t = x[1]
    tau, D = args
    # return jnp.array([x0_t, eval(str(eom[0]))])
    # return jnp.array([x0_t, eval(str(eom))])
    x0_tt = (1/zeta_expr_lambda(x0_epsed))*(tau - D*x0_t + delta_expr_lambda(x0,x0_t,x0_epsed) - eta_expr_lambda(x0_t,x0_epsed)*x0_t )
    # return jnp.array([x0_t, eom_lambda(x0, x0_t, x0_epsed)])
    return jnp.array([x0_t, x0_tt])
    # return x0_t, eom_lambda(x0, x0_t, x0_epsed)#eval(str(eom[0]))

## Training results ##
# true results
q_tt_true = (Xdot[:5000,states_dim//2:].T).copy()
q_t_true = (Xdot[:5000,:states_dim//2].T).copy()
q_true = (X[:5000,:states_dim//2].T).copy()
tau_true = (Tau[:5000,:].T).copy()
# q_tt_true = (Xdot[:5000,states_dim//2:].T).copy()
# q_t_true = (Xdot[:5000,:states_dim//2].T).copy()
# q_true = (X[:5000,:states_dim//2].T).copy()
# tau_true = (Tau[:5000,:].T).copy()

# prediction results
dt = 1e-4  # time step
# time_ = np.arange(0.0, 0.5, dt)
time_ = jnp.arange(0.0, 0.5, dt)
# y_0 = np.array([X[0,0], X[0,1]])
y_0 = jnp.array([X[0,0], X[0,1]])
Xpred, Xdotpred = generate_data(softrobot, time_, y_0, Tau)
q_tt_pred = Xdotpred[:,states_dim//2:].T
q_t_pred = Xdotpred[:,:states_dim//2].T
q_pred = Xpred[:,:states_dim//2].T

# # Check Zeta_expr
# from torch import cos, sin
# # x0_epsed = torch.from_numpy(apply_eps_to_bend_strains(q_pred[0,:], 1e-1))
# x0_epsed = torch.from_numpy(np.arange(-0.7, 0.7, 1e-3, dtype=np.float64))
# zeta_vec = eval(str(zeta_expr))
# # true zeta_expr
# zeta_true = eval('4.4820055191214388e-8 + 0.00044820055191214392/x0_epsed**2 + 0.2689203311472863*cos(0.1*x0_epsed)/x0_epsed**4 + 0.2689203311472863/x0_epsed**4 - 5.378406622945726*sin(0.1*x0_epsed)/x0_epsed**5')
# fig, ax = plt.subplots()
# ax.plot(x0_epsed[:690], zeta_vec[:690], '--r')
# ax.plot(x0_epsed[:690], zeta_true[:690])
# ax.grid(True)

# tau_pred = ELforward(xi_L, Zeta[:,:,:,:5000], Eta[:,:,:,:5000], Delta[:,:,:5000], Xdot[:5000,:], device, D)
# tau_pred = tau_pred.detach().cpu().numpy()
# q_tt_pred_EL = lagrangianforward(xi_L, Zeta[:,:,:,:2000], Eta[:,:,:,:2000], Delta[:,:,:2000], Xdot[:2000,:], device, tau_true, D)

# q_true_torch = torch.from_numpy(q_true).to(device).float()
# q_pred_torch = torch.from_numpy(q_pred).to(device).float()
# loss_q = loss(q_pred_torch, q_true_torch)

# q_tt_true_torch = torch.from_numpy(q_tt_true).to(device).float()
# loss_q_tt = loss(q_tt_pred_EL, q_tt_true_torch)
# q_tt_pred_EL = q_tt_pred_EL.detach().cpu().numpy()

## Validation Results ##
# true results
# q_tt_true_val = (Xdot[300:600,states_dim//2:].T).copy()
# q_t_true_val = (Xdot[300:600,:states_dim//2].T).copy()
# q_true_val = (X[300:600,:states_dim//2].T).copy()

# # prediction results
# tval = np.arange(0.3, 0.6, dt)
# y_0 = np.array([q_pred[0,-1], q_t_pred[0,-1]])
# Xtestpred, Xdottestpred = generate_data(softrobot, tval, y_0)

# ## Concatenate training and test data ##
# # true results
# t = np.concatenate((time_, tval))
# q_tt_true = np.concatenate((q_tt_true, q_tt_true_val), axis=1)
# q_t_true = np.concatenate((q_t_true, q_t_true_val), axis=1)
# q_true = np.concatenate((q_true, q_true_val), axis=1)

# # prediction results
# q_tt_pred = np.concatenate((q_tt_pred,Xdottestpred[:,states_dim//2:].T), axis=1)
# q_t_pred = np.concatenate((q_t_pred, Xtestpred[:,states_dim//2:].T), axis=1)
# q_pred = np.concatenate((q_pred,Xtestpred[:,:states_dim//2].T), axis=1)

## Plotting
t = time_
fig, ax = plt.subplots(3,1)

ax[0].plot(t, q_tt_true[0,:], label='True Model')
ax[0].plot(t, q_tt_pred[0,:], 'r--',label='Predicted Model')
ax[0].set_ylabel('$\ddot{q}$')
# ax[0].vlines(0.4,0,1,transform=ax[0].get_xaxis_transform(),colors='k')
ax[0].set_xlim([0,0.5])
ax[0].grid(True)

ax[1].plot(t, q_t_true[0,:], label='True Model')
ax[1].plot(t, q_t_pred[0,:], 'r--',label='Predicted Model')
ax[1].set_ylabel('$\dot{q}$')
# ax[1].vlines(0.4,0,1,transform=ax[1].get_xaxis_transform(),colors='k')
ax[1].set_xlim([0,0.5])
ax[1].grid(True)

ax[2].plot(t, q_true[0,:], label='True Model')
ax[2].plot(t, q_pred[0,:], 'r--',label='Predicted Model')
ax[2].set_xlabel('Time (s)')
ax[2].set_ylabel('$q$')
# ax[2].vlines(0.4,0,1,transform=ax[2].get_xaxis_transform(),colors='k')
ax[2].set_xlim([0,0.5])
ax[2].grid(True)

Line, Label = ax[0].get_legend_handles_labels()
fig.legend(Line, Label, loc='upper right')
fig.suptitle('Simulation results xL-SINDY - 1 DOF (bending)')

fig.tight_layout()
plt.show()

# fig, ax = plt.subplots(1,1)
# ax.plot(t, tau_true[0,:], label='True Model')
# ax.plot(t, tau_pred[0,:], 'r--',label='Predicted Model')
# ax.set_xlabel('Time (s)')
# ax.set_ylabel('$Tau$')
# ax.set_xlim([0,0.5])
# ax.grid(True)
# plt.show()