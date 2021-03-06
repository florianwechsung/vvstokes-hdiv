from firedrake import *
from functools import reduce

import argparse
import numpy as np
from petsc4py import PETSc

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--nref", type=int, default=1)
parser.add_argument("--k", type=int, default=2)
parser.add_argument("--solver-type", type=str, default="almg")
parser.add_argument("--gamma", type=float, default=1e4)
parser.add_argument("--dr", type=float, default=1e8)
parser.add_argument("--N", type=int, default=10)
parser.add_argument("--case", type=int, default=3)
parser.add_argument("--nonzero-rhs", dest="nonzero_rhs", default=False, action="store_true")
parser.add_argument("--nonzero-initial-guess", dest="nonzero_initial_guess", default=False, action="store_true")
parser.add_argument("--quad", dest="quad", default=False, action="store_true")
parser.add_argument("--itref", type=int, default=0)
parser.add_argument("--w", type=float, default=0.0)
parser.add_argument("--discretisation", type=str, default="hdiv")
args, _ = parser.parse_known_args()


nref = args.nref
dr = args.dr
k = args.k
N = args.N
case = args.case
w = args.w
gamma = Constant(args.gamma)

distp = {"partition": True, "overlap_type": (DistributedMeshOverlapType.VERTEX, 1)}
mesh = RectangleMesh(N, N, 4, 4, distribution_parameters=distp, quadrilateral=args.quad)

mh = MeshHierarchy(mesh, nref, reorder=True, distribution_parameters=distp)

mesh = mh[-1]

if args.quad:
    V = FunctionSpace(mesh, "RTCF", k)
    Q = FunctionSpace(mesh, "DQ", k-1)
else:
    if args.discretisation == "hdiv":
        V = FunctionSpace(mesh, "BDM", k)
        Q = FunctionSpace(mesh, "DG", k-1)
    elif args.discretisation == "cg":
        assert k == 2, "only k=2 is implemented"
        V = VectorFunctionSpace(mesh, "CG", k)
        Q = FunctionSpace(mesh, "DG", k-2)
    else:
        raise ValueError("please specify hdiv or cg for --discretisation")


Z = V * Q
print("dim(Z) = ", Z.dim())
print("dim(V) = ", V.dim())
print("dim(Q) = ", Q.dim())
z = Function(Z)
u, p = TrialFunctions(Z)
v, q = TestFunctions(Z)
bcs = [DirichletBC(Z.sub(0), Constant((0., 0.)), "on_boundary")]

omega = 0.4 #0.4, 0.1
delta = 10 #10, 200
mu_min = Constant(dr**-0.5)
mu_max = Constant(dr**0.5)

def Max(a, b): return (a+b+abs(a-b))/Constant(2)

def chi_n(mesh):
    X = SpatialCoordinate(mesh)
    def indi(ci):
        return 1-exp(-delta * Max(0, sqrt(inner(ci-X, ci-X))-omega/2)**2)
    # indis = [indi(Constant((4*(cx+1)/3, 4*(cy+1)/3))) for cx in range(2) for cy in range(2)]
    indis = []
    np.random.seed(1)
    for i in range(8):
        cx = 2+np.random.uniform(-1,1)
        cy = 2+np.random.uniform(-1,1)
        indis.append(indi(Constant((cx,cy))))
    # Another test:
    # for i in range(4):
    #     cx = 2+np.random.uniform(-1,1)
    #     cy = 2+np.random.uniform(-1,1)
    #     indis.append(indi(Constant((cx,cy))))
    # for i in range(2):
    #     cx = 3+np.random.uniform(-1,1)
    #     cy = 3+np.random.uniform(-1,1)
    #     indis.append(indi(Constant((cx,cy))))
    # for i in range(2):
    #     cx = 3+np.random.uniform(-1,1)
    #     cy = 1+np.random.uniform(-1,1)
    #     indis.append(indi(Constant((cx,cy))))
    # indis.append(indi(Constant((cx,cy))))

    return reduce(lambda x, y : x*y, indis, Constant(1.0))

def mu_expr(mesh):
    return (mu_max-mu_min)*(1-chi_n(mesh)) + mu_min

def mu(mesh):
    Qm = FunctionSpace(mesh, Q.ufl_element())
    return Function(Qm).interpolate(mu_expr(mesh))

File("mu.pvd").write(mu(mesh))

sigma = Constant(100.)
h = CellDiameter(mesh)
n = FacetNormal(mesh)

def diffusion(u, v, mu):
    return (mu*inner(2*sym(grad(u)), grad(v)))*dx \
        - mu * inner(avg(2*sym(grad(u))), 2*avg(outer(v, n))) * dS \
        - mu * inner(avg(2*sym(grad(v))), 2*avg(outer(u, n))) * dS \
        + mu * sigma/avg(h) * inner(2*avg(outer(u,n)),2*avg(outer(v,n))) * dS

def nitsche(u, v, mu, bid, g):
    my_ds = ds if bid == "on_boundary" else ds(bid)
    return -inner(outer(v,n),2*mu*sym(grad(u)))*my_ds \
           -inner(outer(u-g,n),2*mu*sym(grad(v)))*my_ds \
           +mu*(sigma/h)*inner(v,u-g)*my_ds

F = diffusion(u, v, mu_expr(mesh))
for bc in bcs:
    if "DG" in str(bc._function_space):
        continue
    g = bc.function_arg
    bid = bc.sub_domain
    F += nitsche(u, v, mu_expr(mesh), bid, g)

F += - p * div(v) * dx(degree=2*(k-1)) - div(u) * q * dx(degree=2*(k-1))
F += -10 * (chi_n(mesh)-1)*v[1] * dx
if args.nonzero_rhs:
    divrhs = SpatialCoordinate(mesh)[0]-2
else:
    divrhs = Constant(0)
F += divrhs * q * dx(degree=2*(k-1))

Fgamma = F + Constant(gamma)*inner(div(u)-divrhs, div(v))*dx(degree=2*(k-1))

if case < 4:
    a = lhs(Fgamma)
    l = rhs(Fgamma)
elif case == 4:
    # Unaugmented system
    a = lhs(F)
    l = rhs(F)

    # Form BTWB
    M = assemble(a, bcs=bcs)
    A = M.M[0, 0].handle
    B = M.M[1, 0].handle
    ptrial = TrialFunction(Q)
    ptest  = TestFunction(Q)
    W = assemble(Tensor(inner(ptrial, ptest)*dx).inv).M[0,0].handle
    BTW = B.transposeMatMult(W)
    BTW *= args.gamma
    BTWB = BTW.matMult(B)
elif case == 5:
    # Unaugmented system
    a = lhs(F)
    l = rhs(F)

    # Form BTWB
    M = assemble(a, bcs=bcs)
    A = M.M[0, 0].handle
    B = M.M[1, 0].handle
    ptrial = TrialFunction(Q)
    ptest  = TestFunction(Q)
    W = assemble(Tensor(1.0/mu(mh[-1])*inner(ptrial, ptest)*dx).inv).M[0,0].handle
    # W = assemble(Tensor(inner(ptrial, ptest)*dx).inv).M[0,0].handle
    BTW = B.transposeMatMult(W)
    BTW *= args.gamma
    BTWB = BTW.matMult(B)

elif case == 6:
    # Unaugmented system
    a = lhs(F)
    l = rhs(F)

    # Form BTWB
    M = assemble(a, bcs=bcs)
    A = M.M[0, 0].handle
    B = M.M[1, 0].handle
    ptrial = TrialFunction(Q)
    ptest  = TestFunction(Q)
    W1 = assemble(Tensor(1.0/mu(mh[-1])*inner(ptrial, ptest)*dx).inv).M[0,0].handle
    W2 = assemble(Tensor(inner(ptrial, ptest)*dx).inv).M[0,0].handle
    W = W1*w + W2*(1-w)
    BTW = B.transposeMatMult(W)
    BTW *= args.gamma
    BTWB = BTW.matMult(B)
else:
    raise ValueError("Unknown type of preconditioner %i" % case)

""" Demo on how to get the assembled """
# M = assemble(a, bcs=bcs)
# A = M.M[0, 0].handle # A is now a PETSc Mat type
# B = M.M[1, 0].handle

# Eigenvalue analysis
M = assemble(a, bcs=bcs)
Agamma = M.M[0, 0].handle # A is now a PETSc Mat type
B      = M.M[1, 0].handle
if case == 4 or case == 5 or case == 6:
    Agammanp = Agamma[:,:] + BTWB[:,:]
else:
    Agammanp = Agamma[:, :] # obtain a dense numpy matrix
Bnp      = B[:, :]

## Schur complement of original Sgamma
Sgamma = -np.matmul(np.matmul(Bnp, np.linalg.inv(Agammanp)), Bnp.transpose())

## Schur complement of original S
# Form -BTAinvB
M = assemble(lhs(F), bcs=bcs)
A = M.M[0, 0].handle
Anp = A[:, :] # obtain a dense numpy matrix
S = -np.matmul(np.matmul(Bnp, np.linalg.inv(Anp)), Bnp.transpose())

## Preconditioner of Sgamma
pp = TrialFunction(Q)
qq = TestFunction(Q)

#-M_p(1/nu)^{-1}
mu_fun= mu(mh[-1])
viscmass    = assemble(Tensor(-1.0/mu_fun*inner(pp, qq)*dx))
viscmassinv = assemble(Tensor(-1.0/mu_fun*inner(pp, qq)*dx).inv)
viscmass    = viscmass.petscmat
viscmassinv = viscmassinv.petscmat

#-M_p
massinv = assemble(Tensor(-inner(pp, qq)*dx).inv)
massinv = massinv.petscmat

# Comparison -M_p(1/nu)^{-1}, -M_p^{-1}
MpinvS   = np.matmul(massinv[:,:], S)
eigval, eigvec = np.linalg.eig(MpinvS)
print("-Mp: ")
print("[", np.partition(eigval, 2)[1], ", ", max(eigval), "]")
Amu = max(eigval)
amu = np.partition(eigval, 2)[1]
print("Amu = ", Amu)
print("amu = ", amu)
print("MpinvS: ", (args.gamma + 1)/(args.gamma + 1/amu), (args.gamma + 1)/(args.gamma + 1/Amu))

MpmuinvS = np.matmul(viscmassinv[:,:], S)
eigval, eigvec = np.linalg.eig(MpmuinvS)
print("-Mp(1/mu): ")
print("[", np.partition(eigval, 2)[1], ", ", max(eigval), "]")
Cmu = max(eigval)
cmu = np.partition(eigval, 2)[1]
cmu = np.real(cmu)
print("Cmu = ", Cmu)
print("cmu = ", cmu)
print("MpmuinvS: ", (args.gamma + 1)/(args.gamma + 1/cmu), (args.gamma + 1)/(args.gamma + 1/Cmu))

a = 1/dr**0.5
A = dr**0.5

if case == 3 or case == 4:
    if args.gamma > 1e-15:
        print("1/a = ", 1.0/a, "gamma = ", args.gamma)
        dmu = 1 - 1.0/a/args.gamma
        w = (1+a*cmu*args.gamma)/(1+a*args.gamma)
        #print("(1+acmugamma)/(1+agamma)", w)
        # w = 0.9
        # T = 1/cmu*w*viscmassinv[:,:] + (1/(a*cmu)*(1-w)+args.gamma)*massinv[:,:]
        # TinvMmu = np.matmul(T, Sgamma)
        # eigval, eigvec = np.linalg.eig(TinvMmu)
        # print("Eq(27)")
        # print("[", np.partition(eigval, 2)[1], ", ", max(eigval), "]")

        Dmu = 1 + (1 - w)/(a*cmu*args.gamma)
        Dmuinv1 = 1.0/(1/cmu + args.gamma/a) + 1.0/(1+1.0/amu/args.gamma) #(9)
        Dmuinv2 = (1.0 + args.gamma/A)/(1.0/cmu + args.gamma/A) #(15)
        dmuinv  = 1.0/(1/Cmu + args.gamma/A) + 1.0/(1.0+a/args.gamma) #(21)
elif case == 5:
    dmu = (args.gamma + 1/Cmu)/(args.gamma + 1)
    Dmu = (args.gamma + 1/cmu)/(args.gamma + 1)
else:
    raise ValueError("Unknown type of preconditioner %i" % case)

## Preconditioned system
# Pinv
if case == 3 or case == 4:
    Pinv = viscmassinv[:,:] + args.gamma*massinv[:,:]
elif case == 5:
    Pinv = (1.0 + args.gamma)*viscmassinv[:,:]
else:
    raise ValueError("Unknown type of preconditioner %i" % case)

PinvSgamma = np.matmul(Pinv, Sgamma)
eigval, eigvec = np.linalg.eig(PinvSgamma)
argsorteigval = np.argsort(eigval)

## Plot eigenvectors
#eigvecQ = FunctionSpace(mesh, "DG", k-1)
#e = Function(eigvecQ)
#print(eigval[argsorteigval[1]])
#print(np.linalg.norm(np.real(eigvec[:,argsorteigval[1]])))
##e.dat.data[:] = np.matmul(massinv[:,:],np.real(eigvec[:, argsorteigval[1]]))
#aaa = np.matmul(massinv[:,:],np.real(eigvec[:, argsorteigval[1]]))
#e.dat.data[:] = aaa
#File(f"e-5-{args.gamma}-1.pvd").write(e)
#
#print(eigval[argsorteigval[2]])
#print(np.linalg.norm(np.real(eigvec[:,argsorteigval[2]])))
#e.dat.data[:] = np.matmul(massinv[:,:],np.real(eigvec[:, argsorteigval[2]]))
##e.dat.data[:] = np.real(eigvec[:, argsorteigval[2]])
#aaa = np.matmul(massinv[:,:],np.real(eigvec[:, argsorteigval[2]]))
#e.dat.data[:] = aaa
#File(f"e-5-{args.gamma}-2.pvd").write(e)
#
#print(eigval[argsorteigval[3]])
#print(np.linalg.norm(np.real(eigvec[:,argsorteigval[3]])))
#e.dat.data[:] = np.matmul(massinv[:,:],np.real(eigvec[:, argsorteigval[3]]))
##e.dat.data[:] = np.real(eigvec[:, argsorteigval[3]])
#aaa = np.matmul(massinv[:,:],np.real(eigvec[:, argsorteigval[3]]))
#e.dat.data[:] = aaa
#File(f"e-5-{args.gamma}-3.pvd").write(e)
#
#print(eigval[argsorteigval[4]])
#print(np.linalg.norm(np.real(eigvec[:,argsorteigval[4]])))
#e.dat.data[:] = np.matmul(massinv[:,:],np.real(eigvec[:, argsorteigval[4]]))
##e.dat.data[:] = np.real(eigvec[:, argsorteigval[4]])
#File(f"e-5-{args.gamma}-4.pvd").write(e)

print("PinvSgamma: ")
print("[", np.partition(eigval, 2)[1], ", ", max(eigval), "]")
##np.save(f"eig-{args.case}-{args.gamma}-{args.dr}.npy", eigval)
if args.gamma > 1e-15:
    print("Estimation: 1/Dmu = ", 1.0/Dmu)
    print("Estimation: (9)  = ", Dmuinv1)
    print("Estimation: (15) = ", Dmuinv2)
    print("Estimation: (21) = ", dmuinv)
    if abs(dmu) > 1e-15:
        print("1/dmu = ", 1.0/dmu)
