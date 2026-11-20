const BOARD_WIDTH = 10;
const BOARD_HEIGHT = 20;
const CELL_SIZE = 20;
let board = Array(BOARD_HEIGHT).fill().map(() => Array(BOARD_WIDTH).fill(null));
let currentPiece = null;
let nextPiece = null;
let heldPiece = null;
let canHold = true;
let score = 0;
let level = 1;
let linesCleared = 0;
let gameOver = false;
let paused = false;
let lastDropTime = Date.now();
let dropInterval = 1000;

const TETROMINOS = [
    [[0, 0], [1, 0], [2, 0], [3, 0]], // I
    [[0, 0], [1, 0], [0, 1], [1, 1]], // O
    [[0, 0], [-1, 0], [1, 0], [1, 1]], // L
    [[0, 0], [-1, 0], [1, 0], [-1, 1]], // J
    [[0, 0], [-1, 0], [0, 1], [1, 1]], // Z
    [[0, 0], [1, 0], [0, 1], [-1, 1]], // S
    [[0, 0], [-1, 0], [0, 1], [1, 1]] // T
];

function createPiece() {
    const randomIndex = Math.floor(Math.random() * TETROMINOS.length);
    return { type: randomIndex, cells: [...TETROMINOS[randomIndex]], x: 4, y: 0 };
}

function renderBoard() {
    const gameBoard = document.getElementById('game-board');
    while (gameBoard.firstChild) {
        gameBoard.removeChild(gameBoard.lastChild);
    }
    for (let row = 0; row < BOARD_HEIGHT; row++) {
        for (let col = 0; col < BOARD_WIDTH; col++) {
            const cellDiv = document.createElement('div');
            if (board[row][col]) {
                cellDiv.style.backgroundColor = getRandomNeonColor();
            } else {
                cellDiv.style.backgroundColor = 'black';
            }
            cellDiv.style.width = `${CELL_SIZE}px`;
            cellDiv.style.height = `${CELL_SIZE}px`;
            cellDiv.style.position = 'absolute';
            cellDiv.style.left = `${col * CELL_SIZE}px`;
            cellDiv.style.top = `${row * CELL_SIZE}px`;
            gameBoard.appendChild(cellDiv);
        }
    }
}

function renderNextPiece(piece) {
    const nextPieceContainer = document.getElementById('next-piece');
    while (nextPieceContainer.firstChild) {
        nextPieceContainer.removeChild(nextPieceContainer.lastChild);
    }
    piece.cells.forEach((cell) => {
        const cellDiv = document.createElement('div');
        cellDiv.style.backgroundColor = getRandomNeonColor();
        cellDiv.style.width = `${CELL_SIZE}px`;
        cellDiv.style.height = `${CELL_SIZE}px`;
        cellDiv.style.position = 'absolute';
        cellDiv.style.left = `${cell[0] * CELL_SIZE}px`;
        cellDiv.style.top = `${cell[1] * CELL_SIZE}px`;
        nextPieceContainer.appendChild(cellDiv);
    });
}

function renderHeldPiece(piece) {
    const holdPieceContainer = document.getElementById('hold-piece');
    while (holdPieceContainer.firstChild) {
        holdPieceContainer.removeChild(holdPieceContainer.lastChild);
    }
    if (!piece) return;
    piece.cells.forEach((cell) => {
        const cellDiv = document.createElement('div');
        cellDiv.style.backgroundColor = getRandomNeonColor();
        cellDiv.style.width = `${CELL_SIZE}px`;
        cellDiv.style.height = `${CELL_SIZE}px`;
        cellDiv.style.position = 'absolute';
        cellDiv.style.left = `${cell[0] * CELL_SIZE}px`;
        cellDiv.style.top = `${cell[1] * CELL_SIZE}px`;
        holdPieceContainer.appendChild(cellDiv);
    });
}

function updateStats() {
    document.getElementById('score').textContent = `Score: ${score}`;
    document.getElementById('level').textContent = `Level: ${level}`;
    document.getElementById('lines').textContent = `Lines Cleared: ${linesCleared}`;
}

function checkCollision(piece) {
    return piece.cells.some((cell) =>
        !(
            cell[0] >= 0 &&
            cell[0] < BOARD_WIDTH &&
            cell[1] >= 0 &&
            cell[1] < BOARD_HEIGHT &&
            (!board[piece.y + cell[1]] || !board[piece.y + cell[1]][piece.x + cell[0]])
        )
    );
}

function lockPieceToBoard(piece) {
    piece.cells.forEach((cell) => {
        board[piece.y + cell[1]][piece.x + cell[0]] = true;
    });
}

function clearFullRows() {
    let rowsCleared = 0;
    for (let i = BOARD_HEIGHT - 1; i >= 0; i--) {
        if (board[i].every((cell) => cell)) {
            board.splice(i, 1);
            board.unshift(Array(BOARD_WIDTH).fill(null));
            rowsCleared++;
        }
    }
    if (rowsCleared > 0) {
        score += rowsCleared ** 2 * 100;
        linesCleared += rowsCleared;
        updateStats();
    }
}

function spawnNewPiece() {
    currentPiece = nextPiece ? nextPiece : createPiece();
    nextPiece = createPiece();
    renderNextPiece(nextPiece);
    if (checkCollision(currentPiece)) {
        gameOver = true;
        alert("Game Over!");
        resetGame();
    }
}

function movePiece(dx, dy) {
    const newX = currentPiece.x + dx;
    const newY = currentPiece.y + dy;
    const tempPiece = { ...currentPiece, x: newX, y: newY };
    if (!checkCollision(tempPiece)) {
        currentPiece.x = newX;
        currentPiece.y = newY;
    }
}

function rotatePiece() {
    const rotatedCells = currentPiece.cells.map((cell) => [cell[1], -cell[0]]);
    const tempPiece = { ...currentPiece, cells: rotatedCells };
    if (!checkCollision(tempPiece)) {
        currentPiece.cells = rotatedCells;
    }
}

function holdPiece() {
    if (!canHold) return;
    canHold = false;
    if (!heldPiece) {
        heldPiece = currentPiece;
        spawnNewPiece();
    } else {
        [currentPiece, heldPiece] = [heldPiece, currentPiece];
    }
    renderHeldPiece(heldPiece);
}

function handleInput(event) {
    switch (event.key) {
        case "a":
            movePiece(-1, 0);
            break;
        case "d":
            movePiece(1, 0);
            break;
        case "w":
            rotatePiece();
            break;
        case "s":
            movePiece(0, 1);
            break;
        case " ":
            while (!checkCollision({ ...currentPiece, y: currentPiece.y + 1 })) {
                currentPiece.y++;
            }
            break;
        case "p":
            togglePause();
            break;
        default:
            break;
    }
}

function tick() {
    if (Date.now() - lastDropTime > dropInterval && !paused && !gameOver) {
        movePiece(0, 1);
        lastDropTime = Date.now();
    }
    if (checkCollision(currentPiece)) {
        lockPieceToBoard(currentPiece);
        clearFullRows();
        spawnNewPiece();
    }
    renderBoard();
    requestAnimationFrame(tick);
}

function startGame() {
    board = Array(BOARD_HEIGHT).fill().map(() => Array(BOARD_WIDTH).fill(null));
    score = 0;
    level = 1;
    linesCleared = 0;
    gameOver = false;
    paused = false;
    lastDropTime = Date.now();
    dropInterval = 1000;
    spawnNewPiece();
    updateStats();
    renderBoard();
    renderNextPiece(nextPiece);
    renderHeldPiece(heldPiece);
    tick();
}

function resetGame() {
    board = Array(BOARD_HEIGHT).fill().map(() => Array(BOARD_WIDTH).fill(null));
    score = 0;
    level = 1;
    linesCleared = 0;
    gameOver = false;
    paused = false;
    lastDropTime = Date.now();
    dropInterval = 1000;
    updateStats();
    renderBoard();
    renderNextPiece(nextPiece);
    renderHeldPiece(heldPiece);
}

function togglePause() {
    paused = !paused;
    if (paused) {
        document.getElementById('status').textContent = 'Paused';
    } else {
        document.getElementById('status').textContent = '';
    }
}

document.addEventListener('keydown', handleInput);
document.getElementById('start-button').addEventListener('click', startGame);
document.getElementById('pause-button').addEventListener('click', togglePause);
document.getElementById('restart-button').addEventListener('click', resetGame);

function getRandomNeonColor() {
    const colors = ['cyan', 'magenta', 'yellow', 'lime'];
    return colors[Math.floor(Math.random() * colors.length)];
}

window.onload = () => {
    startGame();
}

renderBoard();
updateStats();
tick();
